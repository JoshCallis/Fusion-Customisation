"""
Sketch Clipboard
────────────────
One hotkey that copies a whole sketch (or part of one) and pastes it into
another component, preserving every internal constraint, every dimension, the
world position, and the link to the origin.

DISPATCH — the command decides copy vs paste from the current selection:

    a Sketch is selected          → COPY the whole sketch
    sketch entities are selected  → COPY just those entities
    a Profile is selected         → COPY the sketch curves that bound it
    a Component/Occurrence        → PASTE into it
    nothing is selected           → PASTE into the active component
                                    (or into the sketch being edited, if any)

Copy takes geometry whether or not it is fully constrained (it used to refuse
anything that wasn't). Whatever IS tied to the origin, projections or fixed geometry
keeps that tie through the closure below; an under-defined part pastes where it sits
now, just as free to move as it was in the source.

Also available as two standalone actions (SketchClipboardCopyCmd /
SketchClipboardPasteCmd, declared through fusion_ui/hotkeys.py) for a key like
Cmd+V that should always mean "paste" — see _declare_actions.

The clipboard survives an add-in reload and a document reload: it is stored as
entity tokens in Shared State.json, not as live object references.

A paste places whichever was taken LAST — a sketch copied here or a parameter cut
in the Browser window (Clipboard Owner.json; see "Who owns ⌘V" below).

RESULTS ARE A HUD, NOT A DIALOG
────────────────────────────────
Nothing here puts a dialog on screen on its own. Success flashes a silent tick;
failure flashes a cross that fades the same way if left alone, but opens the
error in a dialog if it's clicked (see hud_flash in ~/bin and _hud below).

PARTIAL COPIES ARE CLOSED OVER THEIR CONSTRAINTS, AUTOMATICALLY
─────────────────────────────────────────────────────────────
A copied entity keeps its position only if every constraint/dimension on it
either stays inside the copied set or lands on an anchor (the sketch origin, a
projection, or fixed geometry).  So a copy of selected entities first walks the
constraint graph out from the selection, stopping at anchors, and pulls in
whatever it reaches (_close_selection) — without asking, on the same
never-pull-in-less-than-position-depends-on principle the closure already
documents.  Whatever ends up copied, selection and additions alike, is left
selected afterward so an addition is visible in the viewport rather than
reported in a prompt.

WHY THIS WORKS WHEN COPY/PASTE-BY-HAND DOESN'T
──────────────────────────────────────────────
Sketch.copy(entities, transform, targetSketch) already carries over every
geometric constraint and dimension *between the copied entities*.  Measured on
a 5-constraint source sketch: 4 of 5 survived plus the dimension.  The single
casualty is any constraint that references something OUTSIDE the copied set —
in practice the source sketch's origin point, i.e. exactly the anchor that ties
the sketch to the world.

So after the copy this add-in rebuilds every one of those dropped references
(_relink_external):

  • constraints/dimensions against the source sketch's own origin point are
    reattached to the target's origin point;
  • projections are rebuilt first (_reproject_all) by projecting the same
    underlying entities into the target, and anything dimensioned or constrained
    to a projection is reattached to the new one.

That second case matters because the Auto-Project Origin add-in puts a projected
top-level origin into every new sketch, so most sketches now measure from a
projection rather than from the sketch's own origin.  If a projection cannot be
resolved, a fresh projection of the top-level origin is made and used instead.

PLACEMENT: THE TRANSFORM MUST COME OUT IDENTITY
──────────────────────────────────────────────
Sketch.copy degrades badly under a non-identity transform — it refuses one with
any out-of-plane component ("Invalid transform. Only uniform scaling is
supported.") and silently drops horizontal/vertical constraints and dimensions
under a rotation or reflection.  Measured against a mirrored component: 5
constraints + 1 dimension became 3 constraints + 0 dimensions.

So placement never goes into the copy transform.  It goes into the target
sketch's PLANE, chosen/built so that the residual transform is exactly the
identity:

  • If the wanted frame matches one of the target component's origin planes,
    use it directly (the common case — no extra geometry).
  • Otherwise build a three-point construction plane through the wanted origin,
    X and Y points.  Verified to reproduce the wanted frame exactly, including
    for mirrored components, where no origin plane can.

MEASURED FACTS THIS CODE RELIES ON (all verified live, see git history)
──────────────────────────────────────────────────────────────────────
  • world frame of a sketch == occ.transform2 ∘ sketch.transform, exactly.
    Cross-checked against SketchPoint.worldGeometry on 36 real sketches: 0 error.
  • sketch.transform for a sketch on an origin plane is a component-independent
    constant (see _ORIGIN_PLANE_FRAMES).
  • sketch.referencePlane raises InternalValidationError for a sketch nested in
    an occurrence — never use it.  sketch.transform always works.
  • Sketch.copy returns the new entities in a DIFFERENT ORDER than they were
    passed, so mapping source→copy by ordinal is silently wrong.  Map by
    geometry instead (_geo_key).

BOUNDS-AWARE RETARGETING, WHEN PASTING ACROSS COMPONENTS
──────────────────────────────────────────────────────
A dimension measured from a bounds point (Bounds.py) needs two more things
fixed up than a plain origin-anchored one does, both only when the source and
target sketches belong to different components:

  • a re-projected bounds point should come from the TARGET component's own
    bounds sketch, not the source's — projecting the source's would create the
    cross-component reference Bounds.py exists to avoid. _equivalent_bounds_point
    finds the target's matching corner/edge point by classifying which face
    (or the midline) each axis sits on, and _reproject_all projects that one
    instead when a match exists.
  • an expression naming a bounds parameter (e.g. "DrawerVeneerBack - 20")
    should name the target's equivalent ("DrawerWingBack - 20") once pasted.
    _bounds_rename_map builds the {source name: target name} table and
    _rewrite_expression applies it, whole-word, to every dimension the paste
    touches — both the ones _relink_external recreates (which also need their
    expression restored outright: addDistanceDimension et al only take two
    entities and a text position, so what comes back always measures as a bare
    number until the source's expression is copied across) and the ones
    Sketch.copy carried over unchanged (_fixup_internal_dimensions), in case
    copy left one of those as a number too or it still names the source.
"""

import adsk.core, adsk.fusion, traceback, os, math, re, subprocess, sys, time, json
import urllib.request
import importlib.util
import types as _types

LOG = '/tmp/sketch_clipboard.log'
HUD_FLASH = '/Users/josh/bin/hud_flash'


def _hud(kind, message=None):
    """A soft, uncoloured tick/cross flashed on screen — fire and forget, since
    waiting on it would hold up the command for the ~1s the HUD is on screen.

    A 'fail' with a message is clickable: it fades out like any other HUD if
    left alone, but clicking it swaps the fade for the message in a dialog.
    Nothing shows on its own — success is silent and an error only surfaces if
    the cross is clicked."""
    try:
        args = [HUD_FLASH, kind]
        if message:
            args.append(message)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fss = _load('fss', 'Shared State.py')
fvs = _load('fvs', 'Visibility Snapshot.py')
skp = _load('skp', 'Sketch Picks.py')

# The per-component bounds record and its sketches — see Bounds.py. Loaded the
# way every add-in here loads a sibling module; a failure here must never stop
# an ordinary copy/paste, which worked long before bounds existed.
try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None

CMD_ID    = 'SketchClipboardCmd'
CMD_NAME  = 'Sketch Clipboard'
CMD_DESC  = ('Copy the selected sketch, sketch geometry, or profile; with nothing '
             'selected, paste it into the active component.')
STATE_KEY = 'sketch_clipboard'

# ── Who owns ⌘V ──────────────────────────────────────────────────────────────
# Two things answer a paste: this add-in's sketch clipboard and the Browser window's
# parameter cut (⌘X there). The one taken MOST RECENTLY is the one a paste places —
# a ledger both sides write: this side on every copy (_claim_clipboard), the Browser's
# from fusion_browser/server.py's /uiflags handler the moment a cut starts or ends.
# Its own file, not a Shared State.json key: the Browser writes it from its HTTP
# thread, and Shared State's whole-file read-modify-write isn't safe against a
# second writer (a torn read there would drop this add-in's own clipboard key).
#   {"who": "sketch" | "browser", "ts": <epoch s>, "pending": <browser: cut not yet placed>}
OWNER_FILE  = os.path.join(_ADDINS_DIR, 'Clipboard Owner.json')
BROWSER_URL = 'http://127.0.0.1:8732'


def _clipboard_owner():
    try:
        with open(OWNER_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _browser_action(action_id):
    """Hand an action to the Browser window; True if a live window took it.
    Same best-effort POST as Dimension Value's _post_ui_action."""
    try:
        body = json.dumps({'id': action_id}).encode('utf-8')
        req = urllib.request.Request(BROWSER_URL + '/uiaction', data=body,
                                     headers={'Content-Type': 'application/json'})
        return bool(json.loads(urllib.request.urlopen(req, timeout=0.4).read()).get('delivered'))
    except Exception:
        return False


def _claim_clipboard():
    """A copy just landed: the sketch clipboard is now the newest thing, and a
    parameter the Browser was holding for a paste is let go (its rows stop dimming)."""
    try:
        tmp = OWNER_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'who': 'sketch', 'ts': time.time()}, f)
        os.replace(tmp, OWNER_FILE)
    except Exception:
        pass
    _browser_action('BrowserClearCutCmd')


def _browser_owns_paste():
    """True when the newest clipboard is the Browser's — the paste is then its, and
    this add-in must not also place an older sketch. A cut still pending is handed to
    the window to place; one already placed leaves nothing to paste, which says so
    rather than falling back to a sketch copied BEFORE it. A pending cut whose window
    is gone (nothing took the hand-off) falls through to the sketch."""
    o = _clipboard_owner()
    if o.get('who') != 'browser':
        return False
    if o.get('pending'):
        return _browser_action('BrowserPasteCmd')
    _hud('fail', 'The last thing cut was a parameter in the Browser, and it has '
                 'already been pasted. Copy a sketch again to paste it.')
    return True

# ── Copy-only / Paste-only actions ───────────────────────────────────────────
# CMD_ID above decides copy vs paste from the selection, which is what a single
# shared key needs. A key like Cmd+V means "paste" regardless of what happens to
# be selected, so it needs its own command — declared to the shared registry
# (fusion_ui/hotkeys.py) rather than hard-bound, per the standing rule that every
# mappable action goes through the registry so Control Room can put a key on it.
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
COPY_ID   = 'SketchClipboardCopyCmd'
COPY_NAME = 'Sketch Clipboard: Copy'
COPY_DESC = 'Copy the selected sketch, selected sketch geometry, or a selected profile.'
PASTE_ID   = 'SketchClipboardPasteCmd'
PASTE_NAME = 'Sketch Clipboard: Paste'
PASTE_DESC = ('Paste the copied sketch into the selected component or occurrence; '
              'with nothing selected, into the active component (or the sketch '
              'being edited, if any).')


def _hotkeys():
    """The shared action registry, via sys.modules — see Extrude.py for the full
    rationale. One registry and one router for every add-in; re-exec'd in place
    when hotkeys.py changes, since it lives outside the AddIns folder and Reload
    Recent AddIns cannot reach it."""
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod

# Coordinate rounding for source→copy matching, in internal units (cm).
# 1e-6 cm = 10 nanometres: far below any modelling tolerance, far above the
# float noise Sketch.copy introduces.
_TOL  = 1e-6
_NDIG = 6

app = None
ui = None
handlers = []


# ─────────────────────────────────────────────────────────────────────────────
# Matrix helpers
#
# Fusion convention: a.transformBy(b) makes a into (b ∘ a) — b is applied after.
# ─────────────────────────────────────────────────────────────────────────────

def _compose(inner, outer):
    """Return outer ∘ inner as a new matrix."""
    m = inner.copy()
    m.transformBy(outer)
    return m


def _inverse(m):
    inv = m.copy()
    inv.invert()
    return inv


def _frame_of(m):
    """(origin, xdir, ydir) of a coordinate-system matrix."""
    d = m.asArray()
    return (adsk.core.Point3D.create(d[3], d[7], d[11]),
            adsk.core.Vector3D.create(d[0], d[4], d[8]),
            adsk.core.Vector3D.create(d[1], d[5], d[9]))


def _mat_from_frame(origin, xdir, ydir):
    m = adsk.core.Matrix3D.create()
    m.setWithCoordinateSystem(origin, xdir, ydir, xdir.crossProduct(ydir))
    return m


def _matrix_from(values):
    m = adsk.core.Matrix3D.create()
    m.setWithArray(values)
    return m


_IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _is_identity(m, tol=_TOL):
    return all(abs(a - b) <= tol for a, b in zip(m.asArray(), _IDENTITY))


def _same(a, b, tol=_TOL):
    return all(abs(x - y) <= tol for x, y in zip(a.asArray(), b.asArray()))


def _rotation_angle(m):
    d = m.asArray()
    return abs(math.atan2(d[4], d[0]))


def _apply(m, x, y):
    p = adsk.core.Point3D.create(x, y, 0.0)
    p.transformBy(m)
    return p


# Local frame (sketch.transform) a sketch receives on each origin plane.
# Measured and found identical across identity, translated and mirrored
# components, so these are safe constants rather than something to probe.
_ORIGIN_PLANE_FRAMES = {
    'XY': _matrix_from([1, 0, 0, 0,  0, 1, 0, 0,  0, 0, 1, 0,  0, 0, 0, 1]),
    'XZ': _matrix_from([1, 0, 0, 0,  0, 0, 1, 0,  0, -1, 0, 0,  0, 0, 0, 1]),
    'YZ': _matrix_from([0, 0, 1, 0,  0, 1, 0, 0,  -1, 0, 0, 0,  0, 0, 0, 1]),
}


# ─────────────────────────────────────────────────────────────────────────────
# Where a sketch sits in the world
# ─────────────────────────────────────────────────────────────────────────────

def _occ_world(occ):
    if occ is None:
        return adsk.core.Matrix3D.create()
    try:
        return occ.transform2.copy()
    except Exception:
        return adsk.core.Matrix3D.create()


def _world_matrix(native, occ):
    """Exact world coordinate system of *native* seen through *occ*.

    Primary route is analytic (occ.transform2 ∘ sketch.transform), which is
    exact and works on an empty sketch.  When the sketch has enough points we
    also solve the frame from worldGeometry and prefer that on disagreement —
    cheap insurance against proxy transform semantics we have not characterised
    for every way an occurrence can be obtained.
    """
    analytic = _compose(native.transform, _occ_world(occ))
    measured = _measure_world(native if occ is None else _proxy_for(native, occ))
    if measured is not None and not _same(measured, analytic):
        return measured
    return analytic


def _measure_world(sketch):
    """Solve world = O + u·X + v·Y from the sketch's own points, or None."""
    samples = []
    try:
        for pt in sketch.sketchPoints:
            g = pt.geometry
            samples.append((g.x, g.y, pt.worldGeometry))
            if len(samples) >= 24:
                break
    except Exception:
        return None
    if len(samples) < 3:
        return None

    u0, v0, w0 = samples[0]
    chosen = None
    for i in range(1, len(samples)):
        u1, v1, w1 = samples[i]
        for j in range(i + 1, len(samples)):
            u2, v2, w2 = samples[j]
            det = (u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0)
            if abs(det) > 1e-8:
                chosen = (u1, v1, w1, u2, v2, w2, det)
                break
        if chosen:
            break
    if not chosen:
        return None

    u1, v1, w1, u2, v2, w2, det = chosen
    d1 = (w1.x - w0.x, w1.y - w0.y, w1.z - w0.z)
    d2 = (w2.x - w0.x, w2.y - w0.y, w2.z - w0.z)
    a1, b1 = (u1 - u0), (v1 - v0)
    a2, b2 = (u2 - u0), (v2 - v0)
    xv = adsk.core.Vector3D.create(*[(b2 * d1[k] - b1 * d2[k]) / det for k in range(3)])
    yv = adsk.core.Vector3D.create(*[(-a2 * d1[k] + a1 * d2[k]) / det for k in range(3)])
    if xv.length < 1e-9 or yv.length < 1e-9:
        return None
    xv.normalize()
    yv.normalize()
    origin = adsk.core.Point3D.create(
        w0.x - u0 * xv.x - v0 * yv.x,
        w0.y - u0 * xv.y - v0 * yv.y,
        w0.z - u0 * xv.z - v0 * yv.z)
    return _mat_from_frame(origin, xv, yv)


def _proxy_for(sketch, occ):
    if occ is None:
        return sketch
    try:
        return (sketch.nativeObject or sketch).createForAssemblyContext(occ)
    except Exception:
        return sketch


# ─────────────────────────────────────────────────────────────────────────────
# Building the destination plane so the copy transform comes out identity
# ─────────────────────────────────────────────────────────────────────────────

# Normal of each origin plane, in the component's own coordinates.
_ORIGIN_PLANE_NORMALS = {
    'XY': (0.0, 0.0, 1.0),
    'XZ': (0.0, 1.0, 0.0),
    'YZ': (1.0, 0.0, 0.0),
}


def _origin_planes(comp):
    return {'XY': comp.xYConstructionPlane,
            'XZ': comp.xZConstructionPlane,
            'YZ': comp.yZConstructionPlane}


def _plane_for(comp, local):
    """A plane in *comp* on which a sketch receives the frame *local*.

    Returns (planarEntity, extras, note) or (None, [], reason).  *extras* are
    entities that had to be created and must persist, since the plane depends
    on them.

    Two stages, because neither alone is general:
      1. Match the NORMAL using an origin plane, offset along it as needed.
         Preserves axis directions, so what is left is at worst an in-plane
         translation — which Sketch.copy handles without losing anything.
      2. If the axes still disagree (a mirrored component is the case that
         forces this), build a three-point plane.  The three points must be
         real entities, and ConstructionPoints.add refuses a bare Point3D in
         the parametric environment ("Environment is not supported"), so they
         live in a hidden helper sketch.  That sketch is coplanar with the
         stage-1 plane, which is exactly what makes the points expressible.

    Stage 2 only fixes HANDEDNESS, not arbitrary rotation.  Measured: given
    three points describing a 45° frame, Fusion snaps the resulting sketch back
    to axis-aligned, and ignores the origin point entirely; only a reflection
    (the -X case) is honoured.  So a target component rotated by some arbitrary
    angle about the sketch normal leaves an in-plane rotation in the residual,
    which costs horizontal/vertical constraints and dimensions.  The geometry
    still lands in exactly the right world position, and _do_paste says so.
    """
    planes = _origin_planes(comp)
    for name, frame in _ORIGIN_PLANE_FRAMES.items():
        if _same(frame, local):
            return planes[name], [], None

    origin, xdir, ydir = _frame_of(local)
    normal = xdir.crossProduct(ydir)

    base_name = None
    for name, n in _ORIGIN_PLANE_NORMALS.items():
        nv = adsk.core.Vector3D.create(*n)
        if abs(nv.dotProduct(normal)) > 1.0 - 1e-6:
            base_name = name
            offset = origin.x * n[0] + origin.y * n[1] + origin.z * n[2]
            break
    if base_name is None:
        return None, [], ('it is not parallel to any origin plane of "%s"' % comp.name)

    extras = []
    stage1 = planes[base_name]
    if abs(offset) > _TOL:
        stage1 = _offset_plane(comp, planes[base_name], offset, local, extras)
        if stage1 is None:
            return None, [], 'the offset plane could not be placed'

    helper = comp.sketches.add(stage1)
    desired = _compose(local, _inverse(helper.transform))
    if _is_identity(desired) or _is_pure_translation(desired):
        helper.deleteMe()
        return stage1, extras, ('offset %.4g cm from the %s plane' % (offset, base_name)
                                if extras else None)

    # Axes disagree — rebuild the plane through three points in the helper.
    helper.name = 'Sketch Clipboard ref'
    o2, x2, y2 = _frame_of(desired)
    handles = [helper.sketchPoints.add(p) for p in (
        o2,
        adsk.core.Point3D.create(o2.x + x2.x, o2.y + x2.y, 0.0),
        adsk.core.Point3D.create(o2.x + y2.x, o2.y + y2.y, 0.0))]
    try:
        helper.isVisible = False
    except Exception:
        pass
    extras.append(helper)

    inp = comp.constructionPlanes.createInput()
    inp.setByThreePoints(handles[0], handles[1], handles[2])
    plane = comp.constructionPlanes.add(inp)
    plane.name = 'Sketch Clipboard plane'
    try:
        plane.isLightBulbOn = False
    except Exception:
        pass
    extras.append(plane)
    return plane, extras, 'built a construction plane to match the source orientation'


def _offset_plane(comp, base, offset, local, extras):
    """Offset *base* to reach the wanted plane, correcting the sign if needed."""
    for signed in (offset, -offset):
        inp = comp.constructionPlanes.createInput()
        inp.setByOffset(base, adsk.core.ValueInput.createByReal(signed))
        plane = comp.constructionPlanes.add(inp)
        plane.name = 'Sketch Clipboard plane'
        try:
            plane.isLightBulbOn = False
        except Exception:
            pass
        probe = comp.sketches.add(plane)
        residual = _compose(local, _inverse(probe.transform))
        probe.deleteMe()
        if abs(residual.asArray()[11]) <= _TOL:
            extras.append(plane)
            return plane
        plane.deleteMe()
    return None


def _is_pure_translation(m):
    d = m.asArray()
    rot = [d[0], d[1], d[2], d[4], d[5], d[6], d[8], d[9], d[10]]
    ident = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    return all(abs(a - b) <= _TOL for a, b in zip(rot, ident))


# ─────────────────────────────────────────────────────────────────────────────
# Geometric identity keys (source entity → its copy)
# ─────────────────────────────────────────────────────────────────────────────

def _curve_samples(curve):
    """Three points along a curve in sketch space — works for every curve type."""
    try:
        ev = curve.geometry.evaluator
        ok, sp, ep = ev.getParameterExtents()
        if not ok:
            return None
        pts = []
        for t in (sp, (sp + ep) * 0.5, ep):
            ok, p = ev.getPointAtParameter(t)
            if not ok:
                return None
            pts.append(p)
        return pts
    except Exception:
        return None


def _geo_key(entity, xform=None):
    """Rounded-coordinate identity, built so that
    _geo_key(src, R) == _geo_key(copy_of_src) after src.copy(..., R, target)."""
    t = entity.objectType.split('::')[-1]
    pt = adsk.fusion.SketchPoint.cast(entity)
    if pt:
        g = pt.geometry
        p = _apply(xform, g.x, g.y) if xform else g
        return (t, round(p.x, _NDIG), round(p.y, _NDIG))

    curve = adsk.fusion.SketchCurve.cast(entity)
    if curve:
        pts = _curve_samples(curve)
        if pts is None:
            return None
        out = [t]
        for p in pts:
            q = _apply(xform, p.x, p.y) if xform else p
            out.extend((round(q.x, _NDIG), round(q.y, _NDIG)))
        return tuple(out)
    return None


def _build_key_map(sketch):
    """{geo_key: entity}, minus ambiguous duplicates — never guess.

    The target's own originPoint is excluded: it is never the result of a copy,
    and an anchored source point lands exactly on top of it, which would look
    like a collision and lose the very mapping _relink_external needs.
    """
    origin = sketch.originPoint
    table, collisions = {}, set()
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for e in coll:
            if e == origin:
                continue
            # Re-projected geometry is matched through _reference_pairs, never by
            # position. Leaving it in here would collide with copied geometry
            # sitting at the same spot (a projected origin at (0,0) against a
            # point anchored there) and lose both.
            if getattr(e, 'isReference', False):
                continue
            k = _geo_key(e)
            if k is None:
                continue
            if k in table:
                collisions.add(k)
            else:
                table[k] = e
    for k in collisions:
        table.pop(k, None)
    return table


# ─────────────────────────────────────────────────────────────────────────────
# Selection classification
# ─────────────────────────────────────────────────────────────────────────────

def _profile_entities(profiles):
    """The sketch curves that bound *profiles* — every border line of every
    loop, deduplicated by entity token. Fed straight into the same
    copy_entities path a manual curve selection takes, so a profile pulls in
    whatever _close_selection decides those curves need to stay anchored."""
    seen, out = set(), []
    for p in profiles:
        for i in range(p.profileLoops.count):
            loop = p.profileLoops.item(i)
            for j in range(loop.profileCurves.count):
                ent = adsk.fusion.SketchEntity.cast(loop.profileCurves.item(j).sketchEntity)
                if ent is None:
                    continue
                t = _tok(ent)
                if t in seen:
                    continue
                seen.add(t)
                out.append(ent)
    return out


def _classify(design):
    sels = []
    try:
        for i in range(ui.activeSelections.count):
            sels.append(ui.activeSelections.item(i).entity)
    except Exception:
        pass

    sels = skp.whole_sketches(sels)      # a Browser sketch pick: its curves + the Sketch
    if not sels:
        return 'paste_active', None

    profiles = [p for p in (adsk.fusion.Profile.cast(e) for e in sels) if p]
    if profiles and len(profiles) == len(sels):
        ents = _profile_entities(profiles)
        if not ents:
            return 'error', 'Could not find the sketch geometry for the selected profile.'
        parents = {e.parentSketch.entityToken for e in ents}
        if len(parents) > 1:
            return 'error', 'The selected profiles span more than one sketch.'
        return 'copy_entities', ents

    sketches = [s for s in (adsk.fusion.Sketch.cast(e) for e in sels) if s]
    if len(sketches) == 1 and len(sels) == 1:
        return 'copy_sketch', sketches[0]
    if len(sketches) > 1:
        return 'error', 'Select one sketch at a time, not %d.' % len(sketches)

    ents = [e for e in (adsk.fusion.SketchEntity.cast(x) for x in sels) if e]
    if ents and len(ents) == len(sels):
        parents = {e.parentSketch.entityToken for e in ents}
        if len(parents) > 1:
            return 'error', ('The selected geometry spans %d sketches. Select from one.'
                             % len(parents))
        return 'copy_entities', ents

    occs = [o for o in (adsk.fusion.Occurrence.cast(e) for e in sels) if o]
    if len(occs) == 1 and len(sels) == 1:
        return 'paste_into', occs[0]
    comps = [c for c in (adsk.fusion.Component.cast(e) for e in sels) if c]
    if len(comps) == 1 and len(sels) == 1:
        return 'paste_into', comps[0]

    return 'error', ('Nothing here to copy or paste. Select a sketch, some sketch '
                     'geometry, a profile, or a component — or clear the selection '
                     'to paste.')


# ─────────────────────────────────────────────────────────────────────────────
# Selection closure — everything the selection is tied to the origin THROUGH
#
# A partial copy keeps its position only if every constraint and dimension on
# a copied entity either stays inside the copied set or lands on an ANCHOR:
# the sketch's own origin point, projected geometry (both of which paste
# rebuilds via _relink_external), or fixed geometry.  Anything else is
# dropped by Sketch.copy and the geometry floats.
#
# So before copying, walk the constraint graph out from the selection —
# following both "owns" edges (a curve and its endpoints move together) and
# constraint/dimension edges — and stop at anchors without exploring past
# them.  Whatever the walk reaches has to come along; if a branch never
# reaches an anchor, the source sketch itself never tied it down and it is
# reported as floating rather than silently included.
#
# This can pull in more than the strict minimum — anything connected to the
# selection through a chain that also happens to reach the origin comes
# along, even if a shorter anchor path existed elsewhere in the chain — but
# it never pulls in LESS than what position depends on, which is the side to
# err on. The dialog always shows what was added before anything is stored.
# ─────────────────────────────────────────────────────────────────────────────

# Every attribute name a constraint or dimension uses to point at an entity.
_REF_ATTRS = (
    'point', 'entity', 'entityOne', 'entityTwo', 'line', 'lineOne', 'lineTwo',
    'curve', 'curveOne', 'curveTwo', 'pointOne', 'pointTwo', 'midPointCurve',
    'symmetryLine', 'circleOne', 'circleTwo', 'ellipse', 'centerPoint',
    'childCurves', 'parentCurves', 'entities', 'curves', 'surface',
)


def _referenced(obj):
    """Sketch entities a constraint or dimension refers to."""
    out = []
    for name in _REF_ATTRS:
        try:
            v = getattr(obj, name, None)
        except Exception:
            continue
        if v is None:
            continue
        coll = adsk.core.ObjectCollection.cast(v)
        items = [coll.item(i) for i in range(coll.count)] if coll else [v]
        for it in items:
            e = adsk.fusion.SketchEntity.cast(it)
            if e:
                out.append(e)
    return out


def _owned_points(curve):
    """The sketch points that belong to *curve* (endpoints, centre, fit points)."""
    pts = []
    for name in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
        try:
            p = getattr(curve, name, None)
        except Exception:
            p = None
        if p is not None:
            pts.append(p)
    try:
        fit = curve.fitPoints
        pts.extend(fit.item(i) for i in range(fit.count))
    except Exception:
        pass
    return pts


def _tok(e):
    try:
        return e.entityToken
    except Exception:
        return None


def _is_anchor(entity, sketch):
    try:
        if entity == sketch.originPoint:
            return True
    except Exception:
        pass
    return bool(getattr(entity, 'isReference', False) or getattr(entity, 'isFixed', False))


def _close_selection(sketch, entities):
    """Grow *entities* by walking the constraint graph out to anchors.

    Returns (closed, added, floating, references):
      closed     — the entities to copy, selection first, then the additions
      added      — what the walk pulled in beyond the selection itself
      floating   — selected entities whose branch of the walk hit no anchor
      references — projected entities the set is constrained to (tokens)
    """
    by_tok, adj, owner = {}, {}, {}   # owner: point token -> the curve token that owns it

    def node(e):
        t = _tok(e)
        if t is None:
            return None
        if t not in by_tok:
            by_tok[t] = e
            adj[t] = set()
        return t

    def link(a, b):
        ta, tb = node(a), node(b)
        if ta and tb and ta != tb:
            adj[ta].add(tb); adj[tb].add(ta)

    for c in sketch.sketchCurves:
        ct = node(c)
        for p in _owned_points(c):
            link(c, p)
            owner[_tok(p)] = ct
    for coll in (sketch.geometricConstraints, sketch.sketchDimensions):
        for obj in coll:
            refs = _referenced(obj)
            for i in range(1, len(refs)):
                link(refs[0], refs[i])

    anchors = {t for t, e in by_tok.items() if _is_anchor(e, sketch)}
    # Origin and projections are REBUILT by paste (_relink_external /
    # _reproject_all) rather than copied — those never join the copy.  Fixed
    # geometry has no such rebuild; it has to travel with the copy itself, so
    # _refix can re-apply isFixed to its counterpart afterwards.
    rebuilt = {t for t in anchors
               if by_tok[t] == sketch.originPoint or getattr(by_tok[t], 'isReference', False)}
    seeds = [s for s in (node(e) for e in entities) if s]

    visited, floating, references = set(), [], set()
    for seed in seeds:
        if seed in visited:
            continue
        comp, stack, hit_anchor = set(), [seed], False
        while stack:
            t = stack.pop()
            if t in comp:
                continue
            comp.add(t)
            if t in anchors:
                hit_anchor = True
                if t in rebuilt and getattr(by_tok[t], 'isReference', False):
                    references.add(t)
                continue                       # already resolved; don't explore past
            stack.extend(adj.get(t, ()))
        visited |= comp
        if not hit_anchor:
            floating.extend(by_tok[s] for s in seeds if s in comp)

    selected = set(seeds)
    copyable = [t for t in visited if t not in rebuilt]
    closed = [e for e in entities]
    for t in copyable:
        if t not in selected:
            closed.append(by_tok[t])
    # A point that belongs to a copied curve is implied, not "added".
    added = [by_tok[t] for t in copyable
             if t not in selected and owner.get(t) not in visited]
    return closed, added, floating, references


# ─────────────────────────────────────────────────────────────────────────────
# Copy
# ─────────────────────────────────────────────────────────────────────────────

def _do_copy(design, sketch=None, entities=None):
    if entities:
        sketch = entities[0].parentSketch
    occ    = sketch.assemblyContext
    native = sketch.nativeObject or sketch
    world  = _world_matrix(native, occ)

    tokens = refs = None
    closed = None
    if entities:
        natives = [e.nativeObject or e for e in entities]
        # Whatever the selection needs to stay anchored is pulled in without
        # asking — see _close_selection's own header for why that never pulls
        # in less than position depends on.
        closed, _added, _floating, refs = _close_selection(native, natives)
        refs = sorted(refs)
        tokens = []
        for e in closed:
            try:
                tokens.append(e.entityToken)
            except Exception:
                pass
        if not tokens:
            return 'None of the selected geometry could be referenced.'

    fss.set_value(STATE_KEY, {
        'sketch_token':     native.entityToken,
        'sketch_name':      native.name,
        'doc_name':         app.activeDocument.name,
        'world':            list(world.asArray()),
        'entity_tokens':    tokens,
        'reference_tokens': refs,
    })
    _claim_clipboard()

    if closed is not None:
        # Everything that travels — the selection plus whatever the closure
        # added to keep it anchored — stays selected, so an addition is visible
        # in the viewport instead of behind a prompt.
        ui.activeSelections.clear()
        for e in closed:
            try:
                ui.activeSelections.add(_proxy_for_entity(e, sketch))
            except Exception:
                pass
    # A whole-sketch copy leaves the sketch selected exactly as it already was.

    what = ('%d entities from "%s"' % (len(tokens), native.name)) if tokens else '"%s"' % native.name
    return None, 'Copied %s.' % what


def _proxy_for_entity(entity, context_sketch):
    """*entity* seen through the same occurrence as *context_sketch*.

    Selections inside an occurrence are proxies; native entities can't be
    added to the selection there.
    """
    occ = context_sketch.assemblyContext
    if occ is None:
        return entity
    try:
        return entity.createForAssemblyContext(occ)
    except Exception:
        return entity


# ─────────────────────────────────────────────────────────────────────────────
# Paste
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_target(design, explicit):
    """(component, occurrence, open_sketch_or_None)."""
    try:
        if fvs.is_editing_sketch(design):
            sk = fvs.active_edit_sketch(design)
            if sk:
                native = sk.nativeObject or sk
                return native.parentComponent, sk.assemblyContext, native
    except Exception:
        pass

    if explicit is not None:
        occ = adsk.fusion.Occurrence.cast(explicit)
        if occ:
            return occ.component, occ, None
        comp = adsk.fusion.Component.cast(explicit)
        if comp:
            return comp, None, None

    try:
        occ = getattr(design, 'activeOccurrence', None)
    except Exception:
        occ = None
    return (occ.component if occ else design.activeComponent), occ, None


def _resolve_token(design, token):
    try:
        found = design.findEntityByToken(token)
    except Exception:
        return None
    return found[0] if found else None


def _resolve_source_sketch(design, clip):
    """The copied sketch, native. findEntityByToken never finds the sketch being
    edited — editing rolls the marker back to just before it, so the lookup treats
    it as rolled back — yet its entities, and its own entityToken, still match. So a
    copy made inside a sketch and pasted before leaving it falls back to those."""
    sk = adsk.fusion.Sketch.cast(_resolve_token(design, clip['sketch_token']))
    if sk:
        return sk
    try:
        if fvs.is_editing_sketch(design):
            open_sk = fvs.active_edit_sketch(design)
            open_sk = open_sk.nativeObject or open_sk if open_sk else None
            if open_sk and open_sk.entityToken == clip['sketch_token']:
                return open_sk
    except Exception:
        pass
    for tok in clip.get('entity_tokens') or ():
        ent = _resolve_token(design, tok)
        if ent is not None:
            try:
                return ent.parentSketch
            except Exception:
                pass
    return None


def _dim_entities(d):
    """(entityOne, entityTwo) for a dimension, whatever it calls them."""
    for a, b in (('entityOne', 'entityTwo'), ('lineOne', 'lineTwo'),
                 ('pointOne', 'pointTwo')):
        one, two = getattr(d, a, None), getattr(d, b, None)
        if one is not None and two is not None:
            return one, two
    return None, None


def _is_external(entity, src_native):
    """True if Sketch.copy cannot carry a reference to *entity* across."""
    if entity is None:
        return False
    try:
        if entity == src_native.originPoint:
            return True
    except Exception:
        pass
    return bool(getattr(entity, 'isReference', False))


def _recreate_dimension(d, tgt_sketch, one, two):
    """Rebuild dimension *d* in the target against already-resolved entities.

    Returns the new SketchDimension, or None if this kind can't be rebuilt.
    Whatever comes back has no expression yet — addDistanceDimension et al only
    take two entities and a text position — so the caller still has to copy
    *d*'s own expression across; see _relink_external.
    """
    kind = d.objectType.split('::')[-1]
    dims = tgt_sketch.sketchDimensions
    text = d.textPosition
    if kind == 'SketchLinearDimension':
        return dims.addDistanceDimension(one, two, d.orientation, text)
    if kind == 'SketchOffsetDimension':
        return dims.addOffsetDimension(one, two, text)
    if kind == 'SketchAngularDimension':
        return dims.addAngularDimension(one, two, text)
    if kind == 'SketchDistanceBetweenPointAndSurfaceDimension':
        return None
    # Anything else that takes two entities — try the generic distance form,
    # which is what the overwhelming majority of origin-referenced dimensions are.
    return dims.addDistanceDimension(
        one, two, adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, text)


def _bounds_rename_map(design, src_comp, tgt_comp):
    """{source parameter name: target parameter name} for every bounds/span
    parameter a pasted expression might name — empty if bounds.py didn't load
    or *src_comp* and *tgt_comp* are the same component, since there is then
    nothing to rename.
    """
    rename = {}
    if bounds is None or src_comp is None or tgt_comp is None or src_comp == tgt_comp:
        return rename
    for d in bounds.DIRECTIONS:
        s, t = bounds.slot(design, src_comp, d), bounds.slot(design, tgt_comp, d)
        if s and t and s != t:
            rename[s] = t
    for axis in bounds.AXES:
        s, t = bounds.span_slot(design, src_comp, axis), bounds.span_slot(design, tgt_comp, axis)
        if s and t and s != t:
            rename[s] = t
    return rename


def _rewrite_expression(expr, rename):
    """*expr* with every whole-word occurrence of a source parameter name in
    *rename* swapped for its target counterpart. Longest names first, so one
    name being a prefix of another (e.g. "Desk" inside "DeskBack") can't
    clobber the wrong span."""
    if not expr or not rename:
        return expr
    for name in sorted(rename, key=len, reverse=True):
        expr = re.sub(r'\b%s\b' % re.escape(name), rename[name], expr)
    return expr


# Constraint type -> (GeometricConstraints adder, first attr, second attr) for every
# two-entity constraint _relink_external can rebuild against an external entity. Not
# just Coincident: a line parallel/perpendicular/collinear to a projected axis, or a
# point aligned with a projected point, is dropped by Sketch.copy exactly the same way.
_RELINKABLE_CONSTRAINTS = {
    'CoincidentConstraint':       ('addCoincident',       'point',    'entity'),
    'ParallelConstraint':         ('addParallel',         'lineOne',  'lineTwo'),
    'PerpendicularConstraint':    ('addPerpendicular',    'lineOne',  'lineTwo'),
    'CollinearConstraint':        ('addCollinear',        'lineOne',  'lineTwo'),
    'HorizontalPointsConstraint': ('addHorizontalPoints', 'pointOne', 'pointTwo'),
    'VerticalPointsConstraint':   ('addVerticalPoints',   'pointOne', 'pointTwo'),
    'MidPointConstraint':         ('addMidPoint',         'point',    'midPointCurve'),
}


def _two_entity_constraint(g):
    """(adder name, first entity, second entity) for a constraint _relink_external
    knows how to rebuild, else None."""
    spec = _RELINKABLE_CONSTRAINTS.get(g.objectType.split('::')[-1])
    if spec is None:
        return None
    adder, a, b = spec
    try:
        first, second = getattr(g, a), getattr(g, b)
    except Exception:
        return None
    if first is None or second is None:
        return None
    return adder, first, second


def _relink_external(design, src_native, tgt_sketch, xform, key_map, ref_pairs, rename,
                     copied=None):
    """Rebuild the constraints and dimensions Sketch.copy had to drop.

    Those are exactly the ones referencing something outside the copied set: the
    source sketch's own origin point, and any projected geometry.  Everything
    else copy already handled, so this never duplicates its work.

    Every recreated dimension also gets *d*'s own expression copied onto it
    (rewritten through *rename* for the target's bounds parameters) — see
    _recreate_dimension for why that doesn't happen automatically. If the
    rewritten expression fails to apply, the original is tried before giving up
    and leaving the dimension's measured value in place.

    *copied*, when given, is the set of entity tokens that were copied: a
    constraint or dimension whose non-external side isn't in it belongs to geometry
    that stayed behind, so it is none of this relink's business rather than a skip.

    Returns (constraints, dimensions, skipped, expr_restored, unparameterised).
    """
    tgt_origin = tgt_sketch.originPoint
    cons = dims = skipped = expr_restored = unparameterised = 0

    def resolve(entity):
        """The target counterpart of a source entity, or None."""
        if entity is None:
            return None
        try:
            if entity == src_native.originPoint:
                # The target's own origin only stands in for the source's when
                # the two coincide in space.
                return tgt_origin if _is_identity(xform) else None
        except Exception:
            pass
        for s, t in ref_pairs:
            try:
                if entity == s:
                    return t
            except Exception:
                pass
        return key_map.get(_geo_key(entity, xform))

    def stayed_behind(a, b):
        if copied is None:
            return False
        inside = [e for e in (a, b) if not _is_external(e, src_native)]
        return bool(inside) and not any(_tok(e) in copied for e in inside)

    def skip(what, obj, why):
        _log('relink skipped: %s %s — %s' % (what, obj.objectType.split('::')[-1], why))

    gc = tgt_sketch.geometricConstraints
    for g in src_native.geometricConstraints:
        spec = _two_entity_constraint(g)
        if spec is None:
            continue
        adder, first, second = spec
        if not (_is_external(first, src_native) or _is_external(second, src_native)):
            continue                      # Sketch.copy already brought it across
        if stayed_behind(first, second):
            continue
        a, b = resolve(first), resolve(second)
        if a is None or b is None:
            skip('constraint', g, 'no counterpart for %s' % ('first' if a is None else 'second'))
            skipped += 1
            continue
        try:
            getattr(gc, adder)(adsk.fusion.SketchPoint.cast(a) or a, b)
            cons += 1
        except Exception as e:
            skip('constraint', g, e)
            skipped += 1

    for d in src_native.sketchDimensions:
        one, two = _dim_entities(d)
        if one is None or two is None:
            continue
        if not (_is_external(one, src_native) or _is_external(two, src_native)):
            continue                      # Sketch.copy already brought it across
        if stayed_behind(one, two):
            continue
        a, b = resolve(one), resolve(two)
        if a is None or b is None:
            skip('dimension', d, 'no counterpart for %s' % ('first' if a is None else 'second'))
            skipped += 1
            continue
        try:
            new = _recreate_dimension(d, tgt_sketch, a, b)
        except Exception as e:
            # Usually over-constraining, which is a legitimate outcome.
            skip('dimension', d, e)
            new = None
        if new is None:
            skipped += 1
            continue
        dims += 1
        try:
            expr = d.parameter.expression
        except Exception:
            expr = None
        if not expr:
            continue
        rewritten = _rewrite_expression(expr, rename)
        try:
            new.parameter.expression = rewritten
            expr_restored += 1
            continue
        except Exception as e:
            _log('re-linked dimension: rewritten expression %r failed: %s' % (rewritten, e))
        try:
            new.parameter.expression = expr
            expr_restored += 1
        except Exception as e:
            _log('re-linked dimension: original expression %r also failed: %s' % (expr, e))
            unparameterised += 1

    return cons, dims, skipped, expr_restored, unparameterised


def _reference_entities(sketch):
    """[(sketch_entity, entity_it_references)] for every projection in *sketch*."""
    out = []
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for e in coll:
            if not getattr(e, 'isReference', False):
                continue
            try:
                ref = e.referencedEntity
            except Exception:
                ref = None
            out.append((e, ref))
    return out


def _is_root_origin(design, entity):
    try:
        origin = design.rootComponent.originConstructionPoint
        return entity is not None and (getattr(entity, 'nativeObject', None) or entity) == origin
    except Exception:
        return False


def _classify_bounds_point(design, comp, sketch, pt, axes):
    """{axis: 'origin' or a direction word} for one point in one of *comp*'s own
    bounds sketches — which face (or the midline) it sits on, per axis.

    bounds.face_at first, since that is what the point was actually placed
    against; falling back to the sign of the coordinate only when the exact
    face can't be matched (e.g. a bound was moved after the sketch was built).
    """
    try:
        g = sketch.sketchToModelSpace(pt.geometry)
    except Exception:
        return None
    coord = {'X': g.x, 'Y': g.y, 'Z': g.z}
    out = {}
    for axis in axes:
        value = coord[axis]
        if abs(value) < bounds.TOL:
            out[axis] = 'origin'
            continue
        direction = bounds.face_at(design, comp, axis, value)
        if not direction:
            high, low = bounds.high_low(axis)
            direction = high if value > 0 else low
        out[axis] = direction
    return out


def _equivalent_bounds_point(design, referenced, src_comp, tgt_comp):
    """The point on *tgt_comp*'s matching bounds sketch that sits where
    *referenced* does on *src_comp*'s — classified face by face — or None.

    None whenever *referenced* isn't one of the source component's own bounds
    points (bounds.is_bounds_sketch) to begin with, or the target has no
    matching sketch or point: the caller's existing fallback (project the
    original) covers both.
    """
    if bounds is None or referenced is None or src_comp is None or tgt_comp is None:
        return None
    if src_comp == tgt_comp:
        return None                       # nothing to retarget within one component
    pt = adsk.fusion.SketchPoint.cast(referenced)
    if pt is None:
        return None
    try:
        src_sketch = pt.parentSketch
    except Exception:
        return None
    if src_sketch is None or not bounds.is_bounds_sketch(src_sketch):
        return None

    key = bounds.sketch_key(src_sketch)
    spec = next((p for p in bounds.PLANES if p[0] == key), None)
    if spec is None:
        return None
    axes = spec[2]

    want = _classify_bounds_point(design, src_comp, src_sketch, pt, axes)
    if want is None:
        return None

    tgt_sketch = bounds.find_sketch(tgt_comp, key)
    if tgt_sketch is None:
        return None
    for cand in bounds.bounds_points(tgt_sketch):
        if _classify_bounds_point(design, tgt_comp, tgt_sketch, cand, axes) == want:
            return cand
    return None


def _already_projected_point(sketch, entity):
    """The reference SketchPoint in *sketch* that already projects *entity*, or
    None. Same check as bounds._entity_already_projected, adapted to hand back
    the entity instead of a bool so a repeat projection can be reused rather
    than duplicated."""
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
                return p
    except Exception:
        pass
    return None


def _reproject_all(design, src_native, tgt_sketch, only=None):
    """Rebuild the source's projected geometry inside the target sketch.

    Sketch.copy cannot carry projections across: a projection is a live link to
    something OUTSIDE the sketch, so _collect_entities skips it and every
    dimension or constraint hanging off it is dropped.  Projecting the same
    underlying entities into the target restores both the geometry and something
    for _relink_external to reattach to.

    Returns (pairs, projected, failed) where *pairs* maps each source reference
    entity to its target counterpart.  *only* (a set of tokens) restricts the
    work to the projections a partial copy is actually constrained to.

    Note this works even when the copy transform is NOT the identity: the
    re-projected point lands on the same world location as the source's did (the
    destination plane is built to match the source's), and the copied geometry
    keeps its world position too — so a dimension between them survives with its
    original value even in a moved component, where anchoring to the target
    sketch's own origin would be wrong.

    A reference that is one of the SOURCE component's own bounds points is
    special-cased: projecting it as-is would tie the target sketch to another
    component's envelope, which Bounds.py explicitly avoids, so
    _equivalent_bounds_point looks for the target component's matching point
    and that is projected instead (reusing an existing projection of it rather
    than doubling it up). Anything else — or a bounds point with no equivalent
    on the target — falls back to projecting the original, as before.
    """
    src_comp = src_native.parentComponent
    tgt_comp = tgt_sketch.parentComponent
    pairs, projected, failed = [], 0, 0
    for src_ent, referenced in _reference_entities(src_native):
        if only is not None and src_ent.entityToken not in only:
            continue                  # a partial copy never touched this one
        target_ent = None
        equiv = _equivalent_bounds_point(design, referenced, src_comp, tgt_comp)
        if equiv is not None:
            target_ent = _already_projected_point(tgt_sketch, equiv)
            if target_ent is None:
                try:
                    res = tgt_sketch.project(equiv)
                    if res and res.count:
                        target_ent = res.item(0)
                except Exception:
                    target_ent = None
        if target_ent is None and referenced is not None:
            try:
                res = tgt_sketch.project(referenced)
                if res and res.count:
                    target_ent = res.item(0)
            except Exception:
                target_ent = None
        if target_ent is None and (referenced is None or _is_root_origin(design, referenced)):
            # Explicitly asked for: if the origin projection cannot be resolved,
            # make a fresh projection of the top-level origin and use that.
            try:
                res = tgt_sketch.project(design.rootComponent.originConstructionPoint)
                if res and res.count:
                    target_ent = res.item(0)
            except Exception:
                target_ent = None
        if target_ent is None:
            failed += 1
            continue
        pairs.append((src_ent, target_ent))
        projected += 1
    return pairs, projected, failed


def _find_dimension(sketch, kind, a, b):
    """The dimension of *kind* in *sketch* whose two entities are (*a*, *b*) in
    either order, or None."""
    for d in sketch.sketchDimensions:
        if d.objectType.split('::')[-1] != kind:
            continue
        one, two = _dim_entities(d)
        if (one == a and two == b) or (one == b and two == a):
            return d
    return None


def _fixup_internal_dimensions(design, src_native, tgt_sketch, xform, key_map, rename):
    """Bring the expression on every dimension Sketch.copy carried straight
    across (both entities inside the copied set, so _relink_external never
    touches it) into line with the target.

    Matched to its target counterpart by the _geo_keys of its two entities,
    through *xform* since these are still the SOURCE entities — the same
    identity _build_key_map already gives every copied entity — plus dimension
    type, since Sketch.copy does not preserve dimension order either. Covers
    two things a plain copy leaves wrong: an expression that still names a
    source-component bounds parameter (rewritten via *rename*), and a
    dimension whose expression Sketch.copy dropped to a bare number outright.

    Returns the count of expressions changed.
    """
    fixed = 0
    for d in src_native.sketchDimensions:
        one, two = _dim_entities(d)
        if one is None or two is None:
            continue
        if _is_external(one, src_native) or _is_external(two, src_native):
            continue                      # _relink_external's territory
        a, b = key_map.get(_geo_key(one, xform)), key_map.get(_geo_key(two, xform))
        if a is None or b is None:
            continue
        try:
            expr = d.parameter.expression
        except Exception:
            continue
        if not expr:
            continue
        wanted = _rewrite_expression(expr, rename)
        kind = d.objectType.split('::')[-1]
        target = _find_dimension(tgt_sketch, kind, a, b)
        if target is None:
            continue
        try:
            current = target.parameter.expression
        except Exception:
            continue
        if current == wanted:
            continue
        try:
            target.parameter.expression = wanted
            fixed += 1
        except Exception as e:
            _log('carried-over dimension: expression %r failed: %s' % (wanted, e))
    return fixed


def _refix(ents, xform, key_map):
    """Fixed geometry is an anchor in its own right; make sure it stays one."""
    n = 0
    for i in range(ents.count):
        e = ents.item(i)
        if not getattr(e, 'isFixed', False):
            continue
        t = key_map.get(_geo_key(e, xform))
        if t is None or getattr(t, 'isFixed', False):
            continue
        try:
            t.isFixed = True
            n += 1
        except Exception:
            pass
    return n


def _collect_entities(design, src_native, wanted):
    """(ObjectCollection, skipped_reference_count)."""
    ents = adsk.core.ObjectCollection.create()
    skipped = 0
    if wanted:
        seen = set()
        def add(e):
            if e.entityToken not in seen:
                seen.add(e.entityToken)
                ents.add(e)
        for tok in wanted:
            e = adsk.fusion.SketchEntity.cast(_resolve_token(design, tok))
            if e is None:
                continue
            if getattr(e, 'isReference', False):
                skipped += 1
                continue
            add(e)
            for p in _owned_points(e):     # carry the points so constraints hold
                add(p)
    else:
        for c in src_native.sketchCurves:
            if c.isReference:
                skipped += 1
                continue
            ents.add(c)
        for p in src_native.sketchPoints:
            if p == src_native.originPoint or getattr(p, 'isReference', False):
                continue
            ents.add(p)
    return ents, skipped


def _do_paste(design, explicit):
    clip = fss.get_value(STATE_KEY, None)
    if not isinstance(clip, dict) or not clip.get('sketch_token'):
        return 'Nothing copied yet. Select a sketch and press the key to copy it first.'

    src_native = _resolve_source_sketch(design, clip)
    if not src_native:
        return ('The copied sketch "%s" is not in this document (it was copied from "%s").'
                % (clip.get('sketch_name', '?'), clip.get('doc_name', '?')))

    src_world = _matrix_from(clip['world'])
    tgt_comp, tgt_occ, open_sketch = _resolve_target(design, explicit)
    if tgt_comp is None:
        return 'No target component — activate one, or select one, and try again.'

    start_index = design.timeline.markerPosition
    notes = []

    if open_sketch is not None:
        tgt_sketch = open_sketch
        notes.append('merged into the open sketch "%s"' % tgt_sketch.name)
    else:
        # The frame the new sketch must have, in the target component's space.
        local = _compose(src_world, _inverse(_occ_world(tgt_occ)))
        try:
            plane, extras, note = _plane_for(tgt_comp, local)
        except Exception:
            return ('Could not build a plane in "%s" matching where "%s" sits. '
                    'Nothing was changed.\n%s'
                    % (tgt_comp.name, clip.get('sketch_name', '?'), traceback.format_exc()))
        if plane is None:
            return ('"%s" cannot be rebuilt in "%s" — %s. Create the plane you want, '
                    'open a sketch on it, and paste again to merge into it.'
                    % (clip.get('sketch_name', '?'), tgt_comp.name, note))
        if note:
            notes.append(note)
        tgt_sketch = tgt_comp.sketches.add(plane)
        tgt_sketch.name = clip.get('sketch_name') or 'Sketch'

    # Residual: should be identity by construction. Anything else is reported.
    tgt_world = _world_matrix(tgt_sketch, tgt_occ)
    xform = _compose(src_world, _inverse(tgt_world))

    if abs(xform.asArray()[11]) > _TOL or abs(xform.asArray()[10] - 1.0) > _TOL:
        return ('The destination plane is not coplanar with "%s", so Fusion would '
                'reject the copy. Nothing was changed.'
                % clip.get('sketch_name', '?'))
    rot = _rotation_angle(xform)
    if rot > _TOL:
        notes.append('rotated %.2f° to land in place, so horizontal/vertical '
                     'constraints and dimensions may not have survived' % math.degrees(rot))

    ents, skipped_refs = _collect_entities(design, src_native, clip.get('entity_tokens'))
    if ents.count == 0 and skipped_refs == 0:
        return 'Nothing copyable in "%s".' % clip.get('sketch_name', '?')

    # Projections first: they must exist before the re-link can attach anything
    # to them, and Sketch.copy can never carry them across itself.
    only = clip.get('reference_tokens')
    ref_pairs, reprojected, reproject_failed = _reproject_all(
        design, src_native, tgt_sketch, set(only) if only is not None else None)

    before_g = tgt_sketch.geometricConstraints.count
    before_d = tgt_sketch.sketchDimensions.count
    if ents.count:
        try:
            src_native.copy(ents, xform, tgt_sketch)
        except Exception as exc:
            return 'Fusion refused the copy: %s' % exc
    gained_g = tgt_sketch.geometricConstraints.count - before_g
    gained_d = tgt_sketch.sketchDimensions.count - before_d

    rename = _bounds_rename_map(design, src_native.parentComponent, tgt_comp)
    key_map = _build_key_map(tgt_sketch)
    re_cons, re_dims, lost, expr_restored, unparameterised = _relink_external(
        design, src_native, tgt_sketch, xform, key_map, ref_pairs, rename)
    internal_fixed = _fixup_internal_dimensions(
        design, src_native, tgt_sketch, xform, key_map, rename)
    refixed = _refix(ents, xform, key_map)
    restored = re_cons + re_dims
    rebound = expr_restored + internal_fixed
    gained_g += re_cons
    gained_d += re_dims

    # A new sketch is not being edited, but sketches.add() leaves its dimensions,
    # constraints and construction geometry shown — floating in the model. Hide
    # them last, after every dimension is (re)created, the same as the policy does
    # for any sketch you are not in — and its projections and points too, which the
    # policy leaves alone: outside a sketch only its curves and profiles should show,
    # as they do for a finished sketch. A merge into the open sketch keeps its toggles.
    if open_sketch is None:
        fvs._set_sketch_aids(tgt_sketch, False)
        for prop in ('isProjectedGeometryShown', 'arePointsShown'):
            try:
                setattr(tgt_sketch, prop, False)
            except Exception:
                pass
        _log('  aids after paste:', ' '.join(
            '%s=%s' % (p, getattr(tgt_sketch, p)) for p in (
                'areDimensionsShown', 'areConstraintsShown', 'isConstructionGeometryShown',
                'isProjectedGeometryShown', 'arePointsShown')))

    try:
        end_index = design.timeline.markerPosition - 1
        if end_index > start_index:
            grp = design.timeline.timelineGroups.add(start_index, end_index)
            grp.name = 'Paste %s' % (clip.get('sketch_name') or 'sketch')
    except Exception:
        pass

    src_g = src_native.geometricConstraints.count
    src_d = src_native.sketchDimensions.count
    msg = ('Pasted "%s" into %s.\n%d entities, %d/%d constraints, %d/%d dimensions.'
           % (clip.get('sketch_name', '?'), tgt_comp.name,
              ents.count, gained_g, src_g, gained_d, src_d))
    if reprojected:
        msg += '\nRe-projected %d reference%s.' % (
            reprojected, '' if reprojected == 1 else 's')
    if restored:
        bits = []
        if re_cons:
            bits.append('%d constraint%s' % (re_cons, '' if re_cons == 1 else 's'))
        if re_dims:
            bits.append('%d dimension%s' % (re_dims, '' if re_dims == 1 else 's'))
        msg += '\nRe-linked %s to them.' % ' and '.join(bits)
    if refixed:
        msg += '\nRe-fixed %d entit%s.' % (refixed, 'y' if refixed == 1 else 'ies')
    if rebound:
        msg += '\nRe-bound %d expression%s to %s.' % (
            rebound, '' if rebound == 1 else 's', tgt_comp.name)
    if unparameterised:
        msg += '\n%d dimension%s kept as numbers.' % (
            unparameterised, '' if unparameterised == 1 else 's')
    if lost:
        msg += '\n%d external reference%s could not be re-linked.' % (
            lost, '' if lost == 1 else 's')
    if reproject_failed:
        msg += ('\n%d projection%s could not be rebuilt — the geometry it referenced '
                'is not reachable from "%s".'
                % (reproject_failed, '' if reproject_failed == 1 else 's', tgt_comp.name))
    if not restored and not lost and not _is_identity(xform):
        msg += ('\n"%s" origin sits elsewhere in space, so the geometry is positioned '
                'to match the original rather than anchored to this origin.'
                % tgt_comp.name)
    for n in notes:
        msg += '\nNote: %s.' % n
    return None, msg


# ─────────────────────────────────────────────────────────────────────────────
# Command plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _handle_result(result):
    """Never puts a dialog on screen by itself. Success gets a silent tick;
    failure gets a cross that only opens the dialog if it's clicked."""
    if isinstance(result, str):              # error text
        _log('  -> error:', result.split('\n')[0])
        _hud('fail', result)
    elif isinstance(result, tuple):          # (None, success text)
        _log('  -> ok:', result[1].replace('\n', ' | '))
        if result[1] != 'Copy cancelled.':   # a deliberate cancel, not a result
            _hud('ok')


def _dispatch(design, mode, payload):
    """Run whichever of copy/paste `mode` names, and report the outcome."""
    if mode == 'error':
        _hud('fail', payload)
        return
    if mode == 'copy_sketch':
        result = _do_copy(design, sketch=payload)
    elif mode == 'copy_entities':
        result = _do_copy(design, entities=payload)
    elif _browser_owns_paste():
        return
    elif mode == 'paste_into':
        result = _do_paste(design, payload)
    else:
        result = _do_paste(design, None)
    _handle_result(result)


class _OnExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                _log('execute: no design')
                return
            mode, payload = _classify(design)
            _log('execute: mode=%s  selection=%d  activeComp=%s  editingSketch=%s' % (
                mode, ui.activeSelections.count,
                design.activeComponent.name if design.activeComponent else '?',
                fvs.is_editing_sketch(design)))
            _dispatch(design, mode, payload)
        except Exception:
            tb = traceback.format_exc()
            _log('  -> EXCEPTION:', tb.replace('\n', ' | '))
            _hud('fail', 'Sketch Clipboard – Execute error:\n' + tb)


def _run_copy():
    """The Copy-only action: refuses rather than guessing when the selection
    would not mean "copy" to the combined command either."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        mode, payload = _classify(design)
        _log('copy action: mode=%s  selection=%d' % (mode, ui.activeSelections.count))
        if mode not in ('copy_sketch', 'copy_entities'):
            _hud('fail', 'Nothing to copy — select a sketch, some sketch geometry, '
                        'or a profile first.')
            return
        _dispatch(design, mode, payload)
    except Exception:
        tb = traceback.format_exc()
        _log('copy action EXCEPTION:', tb.replace('\n', ' | '))
        _hud('fail', 'Sketch Clipboard – Copy error:\n' + tb)


def _run_paste():
    """The Paste-only action: pastes into whatever the selection targets (or the
    active component), and refuses rather than silently copying if a sketch or
    sketch geometry is selected instead of a paste target."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        mode, payload = _classify(design)
        _log('paste action: mode=%s  selection=%d' % (mode, ui.activeSelections.count))
        if mode in ('copy_sketch', 'copy_entities'):
            _hud('fail', 'A sketch (or sketch geometry) is selected, which copies rather '
                        'than pastes. Clear the selection, or select a component, and '
                        'press Paste again.')
            return
        _dispatch(design, mode, payload)
    except Exception:
        tb = traceback.format_exc()
        _log('paste action EXCEPTION:', tb.replace('\n', ' | '))
        _hud('fail', 'Sketch Clipboard – Paste error:\n' + tb)


def _declare_actions():
    hk = _hotkeys()
    hk.declare(COPY_ID, COPY_NAME, COPY_DESC, owner='Sketch Clipboard', run=_run_copy)
    hk.declare(PASTE_ID, PASTE_NAME, PASTE_DESC, owner='Sketch Clipboard', run=_run_paste)
    hk.attach()


class _OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            # A handler left over from a previous load of this add-in is still
            # attached to the (never-deleted) command definition. Without this
            # guard it would add a SECOND execute handler, and one key press
            # would run the command twice — copying and then immediately
            # pasting, which looks like it "works" but is not what was asked.
            if self not in handlers:
                _log('commandCreated: stale handler ignored')
                return
            on_exec = _OnExecute()
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
            args.command.isAutoExecute = True
        except Exception:
            ui.messageBox('Sketch Clipboard – CommandCreated error:\n' + traceback.format_exc())


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # Find-or-create, never deleteMe: a keyboard shortcut is bound to the
        # command DEFINITION, so deleting and recreating it silently throws the
        # binding away and the hotkey stops doing anything at all.
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_created = _OnCommandCreated()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        _declare_actions()
        _log('=== run() — command ready, shortcut binding preserved ===')
    except Exception:
        if ui:
            ui.messageBox('Sketch Clipboard – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        # Deliberately NOT deleting the command definition — see run().
        handlers.clear()
        try:
            _hotkeys().withdraw('Sketch Clipboard')
        except Exception:
            pass
        _log('=== stop() ===')
    except Exception:
        if ui:
            ui.messageBox('Sketch Clipboard – stop error:\n' + traceback.format_exc())
