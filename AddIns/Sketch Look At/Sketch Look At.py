"""
Sketch Look At
──────────────
Our own "Auto look at sketch" (Preferences ▸ Design). Fusion's turns the camera
square onto a sketch when you open it, but it also switches to orthographic and
leaves you there. This one:

  - on opening a sketch (SketchActivate — double-click, the Browser, Go Down; or
    SketchCreate — a new one), turns the camera square onto it, STAYING IN
    PERSPECTIVE, and frames its geometry so it fills FILL of the viewport — most of
    the screen, with a wide margin. For a standing sketch (XZ, YZ, any plane nearer
    vertical than flat) up is world Z, so the view is never on its side. For a flat
    one (XY, top or bottom) up follows where the camera was: the edge lowest on
    screen stays lowest, snapped to world ±X/±Y — look down at it from +Y and the
    +Y edge is at the bottom. Which side it looks from — see WHICH SIDE below;
  - on leaving it, puts the camera back exactly where it was before you went in —
    unless you ORBITED while in the sketch, in which case the view you orbited to is
    kept. Panning and zooming don't count: the view direction and up vector are
    what's compared, so a pan/zoom still snaps back. Nor does it go back if you
    stayed in the sketch longer than STAY_KEEP_S (2 min) — by then the old view is
    stale and wherever you are is where you're working.

The turn is the first thing entry does: it's made (snapped, redrawn at once) inside
the entry's own terminated handler, from activeEditObject or the timeline marker —
not after the other add-ins' entry work, which on a big document is seconds. A
deferred look still runs after them all and corrects it only if the sketch or its
framing came out different (a new sketch's projected bounds arriving).

Hopping sketch to sketch (opening one while another is open, or Edit Original's
master hop, Go to Source) keeps the ORIGINAL pre-sketch camera — leaving the last one
goes back to where you were before the first. A hop never passes through that camera
(the hop's SketchStop is not an exit), and a hop onto a sketch parallel to the one
left doesn't move the camera at all (_hop_keeps); any other plane is turned to once.

"Toggle Sketch Look At" (ADD-INS panel ▸ UTILITIES) turns it on and off; the state
survives restarts. Turning it on also switches Fusion's own Auto Look At Sketch off,
so the two never fight.

WHICH SIDE
──────────
A sketch made from the plane box against a bound face ("Front 100", "Top 18") is
ANCHORED to that face, and is looked at from that face's side unless you're already
viewing from within FLIP_WITHIN_DEG (60°) of the opposite side — anywhere else,
up to 120° off, still swings round to the anchored side. A symmetric span word
(Width/Depth/Height) is anchored to its high face, which is where it measures from.
Anything else — a midline datum (X 250), an origin plane, a face — isn't anchored,
and is looked at from whichever side of it you're nearer (90° / 90°).

WHAT GETS FRAMED
────────────────
The sketch's own drawn curves (not construction, not projected), plus everything its
dimensions and constraints reach: any point with a dimension or constraint on it
(a projected bounds point a dimension is measured from), every dimension's text,
and where a dimension to a projected or construction LINE meets that line (the
foot of its text on it — the line itself may run off anywhere). A point with neither
— an unused projected corner, a bare origin — is ignored. A sketch with none of this
— a brand-new one — frames everything instead, which is normally the bounds
rectangle Auto-Project Origin has just projected in, i.e. the component's envelope.
A sketch with nothing at all keeps the current distance and only turns square.

Measured live (2026-09-26): Camera.perspectiveAngle is the full VERTICAL field of
view; SketchCurve.boundingBox is in sketch space; setting viewExtents on an
orthographic camera through the API is ignored — so perspective is also the only
mode this could frame correctly anyway.
"""
import adsk.core
import adsk.fusion
import json
import math
import os
import threading
import time
import traceback
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fvs = _load('fvs', 'Visibility Snapshot.py')
try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None                       # no anchors: every sketch is nearest-side

CMD_ID   = 'JoshSketchLookAtTgl'
CMD_NAME = 'Toggle Sketch Look At'
CMD_DESC = ('Look square at a sketch when it opens (in perspective, with a '
            'margin) and put the camera back when it closes, unless you orbited')

LOOK_EVENT = 'JoshSketchLookAtLook'
ENTRY_COMMANDS = frozenset(('SketchActivate', 'SketchCreate'))

FILL = 0.7              # fraction of the limiting viewport dimension the sketch fills
ORBIT_TOL_DEG = 1.0     # direction/up change beyond this = you orbited
ANIM_GRACE_S = 1.0      # leaving before the entry animation settles isn't an orbit
EXIT_FALLBACK_S = 2.0   # see _OnTerminated — SketchStop.isEnabled lags an entry
STAY_KEEP_S = 120.0     # in the sketch (hops included) longer than this = keep the view
STANDING_MAX_TILT_DEG = 45.0  # plane within this of vertical = standing → world Z up
FLIP_WITHIN_DEG = 60.0  # anchored: viewing within this of the far side → look from it

LOG = '/tmp/sketch_look_at.log'
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'Sketch Look At State.json')

_app = None
_ui = None
_handlers = []

# The open sketch session: {'pre': camera dict, 'look': (dir, up) or None, 'at': t,
# 'since': when the first sketch of a hop chain opened}.
# None when no sketch we looked at is open.
_session = None
_entry_is_hop = False
_pending_cmd = None


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ── state ─────────────────────────────────────────────────────────────────────

def _enabled():
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as f:
            return bool(json.load(f).get('enabled', True))
    except Exception:
        return True                     # default ON


def _set_enabled(value):
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'enabled': bool(value)}, f, indent=2)
    except Exception:
        pass


def _native_look_at_off():
    """Fusion's own Auto Look At Sketch would turn ortho and fight ours."""
    try:
        fp = adsk.fusion.FusionProductPreferences.cast(
            _app.preferences.productPreferences.itemByName('Design'))
        if fp and fp.isAutoLookAtSketch:
            fp.isAutoLookAtSketch = False
            _log('switched Fusion\'s own Auto Look At Sketch off')
    except Exception:
        _log('native pref:', traceback.format_exc().replace('\n', ' | '))


# ── camera ────────────────────────────────────────────────────────────────────

def _grab(cam):
    return {'eye': cam.eye.asArray(), 'target': cam.target.asArray(),
            'up': cam.upVector.asArray(), 'type': cam.cameraType,
            'angle': cam.perspectiveAngle}


def _put(saved):
    vp = _app.activeViewport
    cam = vp.camera
    cam.cameraType = saved['type']
    cam.eye = adsk.core.Point3D.create(*saved['eye'])
    cam.target = adsk.core.Point3D.create(*saved['target'])
    cam.upVector = adsk.core.Vector3D.create(*saved['up'])
    try:
        cam.perspectiveAngle = saved['angle']
    except Exception:
        pass
    cam.isFitView = False
    cam.isSmoothTransition = True
    vp.camera = cam


def _unit(v):
    v = v.copy()
    v.normalize()
    return v


def _angle_deg(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, _unit(a).dotProduct(_unit(b))))))


# ── the sketch ────────────────────────────────────────────────────────────────

def _entered_sketch(design, cmd):
    """The sketch just opened. activeEditObject first (a root-level sketch, with its
    assembly context); it raises for one nested in an occurrence, so then the
    timeline marker for a brand-new sketch (it sits just before the marker) or the
    one Go Down recorded at entry."""
    try:
        sk = adsk.fusion.Sketch.cast(design.activeEditObject)
        if sk:
            return sk
    except Exception:
        pass
    if cmd == 'SketchCreate':
        sk = fvs.edited_sketch_from_marker(design)
        if sk:
            return sk
    return fvs.recall_edit_sketch(design)


def _entered_now(design):
    """The sketch just opened, from the entry handler itself — before Go Down has
    recorded it: activeEditObject, else the timeline marker Fusion has just rolled
    to after it. None if neither names one (the deferred look still runs)."""
    try:
        sk = adsk.fusion.Sketch.cast(design.activeEditObject)
        if sk:
            return sk
    except Exception:
        pass
    return fvs.edited_sketch_from_marker(design)


def _occurrence_for(design, comp):
    """Where a native sketch's component sits in the world — the active occurrence
    if that's it (the one you descended into), else its first occurrence."""
    root = design.rootComponent
    if comp.id == root.id:
        return None
    try:
        ao = design.activeOccurrence
        if ao and ao.component.id == comp.id:
            return ao
    except Exception:
        pass
    occs = root.allOccurrencesByComponent(comp)
    return occs.item(0) if occs.count else None


def _world_frame(design, sk):
    """Sketch space → world, as a Matrix3D."""
    native = sk.nativeObject or sk
    m = native.transform.copy()
    occ = sk.assemblyContext if sk.nativeObject else None
    if occ is None:
        occ = _occurrence_for(design, native.parentComponent)
    if occ is not None:
        m.transformBy(occ.transform2)
    return m


def _dimension_feet(curve):
    """Where each dimension on a projected/construction line meets it: the foot of
    the dimension's text on the (unbounded) line. Other curves: nothing."""
    out = []
    try:
        line = adsk.fusion.SketchLine.cast(curve)
        if not line or not curve.sketchDimensions.count:
            return out
        a, b = line.startSketchPoint.geometry, line.endSketchPoint.geometry
        dx, dy = b.x - a.x, b.y - a.y
        ll = dx * dx + dy * dy
        if ll < 1e-12:
            return out
        for d in curve.sketchDimensions:
            t = d.textPosition
            k = ((t.x - a.x) * dx + (t.y - a.y) * dy) / ll
            x, y = a.x + dx * k, a.y + dy * k
            out.append((x, y, x, y))
    except Exception:
        pass
    return out


def _extent(sk):
    """(minx, miny, maxx, maxy) in sketch space, or None. Drawn curves and what
    the dimensions/constraints reach; if none, every curve (a new sketch's
    projected bounds rectangle). See WHAT GETS FRAMED."""
    native = sk.nativeObject or sk
    drawn, every = [], []
    for c in native.sketchCurves:
        try:
            bb = c.boundingBox
        except Exception:
            continue
        box = (bb.minPoint.x, bb.minPoint.y, bb.maxPoint.x, bb.maxPoint.y)
        every.append(box)
        if not c.isConstruction and not c.isReference:
            drawn.append(box)
        else:
            drawn += _dimension_feet(c)
    for p in native.sketchPoints:
        try:
            if p.sketchDimensions.count or p.geometricConstraints.count:
                g = p.geometry
                drawn.append((g.x, g.y, g.x, g.y))
        except Exception:
            continue
    for d in native.sketchDimensions:
        try:
            t = d.textPosition
            drawn.append((t.x, t.y, t.x, t.y))
        except Exception:
            continue
    boxes = drawn or every
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _flatten(v, z_ax):
    """v with its z_ax component removed (z_ax is unit), or None if v is ~normal."""
    k = v.dotProduct(z_ax)
    f = adsk.core.Vector3D.create(v.x - z_ax.x * k, v.y - z_ax.y * k, v.z - z_ax.z * k)
    return _unit(f) if f.length > 1e-6 else None


def _box_wh(frame, ext, up, right):
    """The sketch's extent box's half-width and half-height as they would project onto
    screen axes up/right — the same corner projection _look_at frames the shot with,
    factored out so _flat_up can try it with a candidate up before committing to one."""
    cx, cy = (ext[0] + ext[2]) / 2.0, (ext[1] + ext[3]) / 2.0
    center = adsk.core.Point3D.create(cx, cy, 0)
    center.transformBy(frame)
    half_w = half_h = 0.0
    for x, y in ((ext[0], ext[1]), (ext[2], ext[1]),
                 (ext[0], ext[3]), (ext[2], ext[3])):
        p = adsk.core.Point3D.create(x, y, 0)
        p.transformBy(frame)
        v = center.vectorTo(p)
        half_w = max(half_w, abs(v.dotProduct(right)))
        half_h = max(half_h, abs(v.dotProduct(up)))
    return half_w, half_h


def _flat_up(z_ax, cam, ext=None, frame=None):
    """A flat sketch's up, kept from how you were looking at it: the camera's up
    carried through the smallest turn onto the new view (the "turned" camera up), then
    snapped to the nearest world ±X/±Y in the plane. So the edge that was lowest on
    screen stays lowest — from above and +Y the near (+Y) edge ends up at the bottom;
    from below it was at the top and stays there.

    A long, flat sketch (its box's long/short side ratio over 1.5, from `ext`/`frame`)
    is kept LANDSCAPE: only a candidate that would actually show it wider than tall
    (_box_wh) is offered, and the closer of those two to the turned camera up wins —
    so a portrait-drawn long sketch still turns to match how you were already looking,
    just never onto its narrow side. A roughly square sketch, or no ext to measure
    (nothing drawn yet), keeps today's behaviour: the closest candidate outright."""
    look = z_ax.copy()
    look.scaleBy(-1)
    fwd = cam.eye.vectorTo(cam.target)
    turned = _unit(cam.upVector)
    if fwd.length > 1e-9 and _angle_deg(fwd, look) > 1e-3:
        m = adsk.core.Matrix3D.create()
        if m.setToRotateTo(_unit(fwd), look):
            turned.transformBy(m)

    candidates = []
    for ax in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        a = _flatten(adsk.core.Vector3D.create(*ax), z_ax)
        if a is None:
            continue
        d = turned.dotProduct(a)
        if d < 0:
            a.scaleBy(-1)
            d = -d
        candidates.append((d, a))

    long_short = None
    if ext is not None and frame is not None:
        w, h = ext[2] - ext[0], ext[3] - ext[1]
        short = min(w, h)
        if short > 1e-9:
            long_short = max(w, h) / short

    if long_short is not None and long_short > 1.5:
        landscape = [(d, a) for d, a in candidates
                     if _box_wh(frame, ext, a, a.crossProduct(z_ax))[0] >
                        _box_wh(frame, ext, a, a.crossProduct(z_ax))[1]]
        if landscape:
            candidates = landscape

    return max(candidates, key=lambda t: t[0])[1] if candidates else None


def _camera_up(y_ax, z_ax, cam, ext=None, frame=None):
    """A standing sketch (XZ, YZ, anything nearer vertical than flat) gets world Z
    as up, flattened into its plane, so the view is never on its side whatever way
    the sketch's own axes point. A flat one keeps the screen orientation you came
    from, kept landscape for a long sketch (_flat_up), falling back to its own Y."""
    along = z_ax.z                      # world Z · sketch normal (z_ax is unit)
    if abs(along) >= math.sin(math.radians(STANDING_MAX_TILT_DEG)):
        return _flat_up(z_ax, cam, ext, frame) or _unit(y_ax)
    return _unit(adsk.core.Vector3D.create(-z_ax.x * along, -z_ax.y * along,
                                           1.0 - z_ax.z * along))


def _anchor(design, sk):
    """(face name, world vector out through that face) for a sketch the plane box
    made against a bound face, else None. See WHICH SIDE."""
    if bounds is None or bounds.planes is None:
        return None
    try:
        word, _rest = bounds.planes.split_input(bounds.sketch_datum_text(sk))
        if not word:
            return None
        native = sk.nativeObject or sk
        d = bounds.find_datum(design, native.parentComponent, word)
        if not d or d['kind'] != 'face':
            return None                 # a midline has no side
        comp_v = [0.0, 0.0, 0.0]
        comp_v[bounds.AXES.index(d['axis'])] = float(d['sign'])
        v = adsk.core.Vector3D.create(*comp_v)
        occ = sk.assemblyContext if sk.nativeObject else None
        if occ is None:
            occ = _occurrence_for(design, native.parentComponent)
        if occ is not None:
            v.transformBy(occ.transform2)
        return d['word'], v
    except Exception:
        _log('anchor:', traceback.format_exc().replace('\n', ' | '))
        return None


def _side(design, sk, cam, z_ax):
    """The sketch normal to look from (z_ax or its reverse), and why. See WHICH SIDE."""
    view = cam.target.vectorTo(cam.eye)
    if view.length < 1e-9:
        return z_ax, 'no view'
    anchor = _anchor(design, sk)
    toward = view.dotProduct(z_ax)
    if anchor is not None and abs(anchor[1].dotProduct(z_ax)) > 1e-6:
        home = z_ax.copy()
        if anchor[1].dotProduct(z_ax) < 0:
            home.scaleBy(-1)
        away = home.copy()
        away.scaleBy(-1)
        if _angle_deg(view, away) < FLIP_WITHIN_DEG:
            return away, 'anchored %s, flipped' % anchor[0]
        return home, 'anchored %s' % anchor[0]
    n = z_ax.copy()
    if toward < 0:
        n.scaleBy(-1)
    return n, 'nearest side'


def _look_at(design, sk, now=False):
    """Square onto sk, perspective, framed. Returns ((dir, up), ext). `now`: snap
    and redraw at once rather than animate — the entry handler's look, which the
    rest of the entry work (other add-ins' handlers) would otherwise hold up."""
    t0 = time.time()
    vp = _app.activeViewport
    cam = vp.camera
    frame = _world_frame(design, sk)
    origin, _x, y_ax, z_ax = frame.getAsCoordinateSystem()
    z_ax, side = _side(design, sk, cam, _unit(z_ax))
    # Computed before up, now: a flat sketch's up wants to know the box's shape too
    # (_camera_up -> _flat_up's landscape rule).
    ext = _extent(sk)
    up = _camera_up(y_ax, z_ax, cam, ext, frame)
    right = up.crossProduct(z_ax)       # screen right, looking along -z_ax

    tan_v = math.tan(cam.perspectiveAngle / 2.0)
    tan_h = tan_v * vp.width / max(1, vp.height)
    dist = None
    if ext:
        cx, cy = (ext[0] + ext[2]) / 2.0, (ext[1] + ext[3]) / 2.0
        center = adsk.core.Point3D.create(cx, cy, 0)
        center.transformBy(frame)
        # The box's corners measured along the screen axes — up may not be the
        # sketch's Y, so its width and height aren't simply the box's.
        half_w, half_h = _box_wh(frame, ext, up, right)
        if half_w > 1e-6 or half_h > 1e-6:
            dist = max(half_h / (tan_v * FILL), half_w / (tan_h * FILL))
    else:
        # Nothing to frame: square on to where you're already looking.
        t = cam.target
        off = origin.vectorTo(t).dotProduct(z_ax)
        center = adsk.core.Point3D.create(t.x - z_ax.x * off, t.y - z_ax.y * off,
                                          t.z - z_ax.z * off)
    if dist is None:
        dist = cam.eye.distanceTo(cam.target)

    cam.cameraType = adsk.core.CameraTypes.PerspectiveCameraType
    cam.target = center
    cam.eye = adsk.core.Point3D.create(center.x + z_ax.x * dist,
                                       center.y + z_ax.y * dist,
                                       center.z + z_ax.z * dist)
    cam.upVector = up
    cam.isFitView = False
    cam.isSmoothTransition = not now
    vp.camera = cam
    if now:
        try:
            vp.refresh()
        except Exception:
            pass
    look_dir = z_ax.copy()
    look_dir.scaleBy(-1)
    _log('look at "%s": ext=%s dist=%.2f up=%s%s%s %d ms' % (
        sk.name, ext, dist, tuple(round(c, 3) for c in up.asArray()),
        ' (%s)' % side, ' now' if now else '', (time.time() - t0) * 1000))
    return (look_dir, up.copy()), ext


def _same_plane(design, a, b):
    """Whether sketches a and b face the same way (parallel planes, either side)."""
    try:
        za = _world_frame(design, a).getAsCoordinateSystem()[3]
        zb = _world_frame(design, b).getAsCoordinateSystem()[3]
        d = _angle_deg(za, zb)
        return min(d, 180.0 - d) < ORBIT_TOL_DEG
    except Exception:
        return False


def _hop_keeps(design, sk):
    """A hop onto a sketch parallel to the one being left keeps the camera exactly
    where it is — the hop should feel like staying in the sketch, not a re-entry.
    Notes the sketch on the session so the deferred look leaves it be."""
    if not (_session and _session.get('hop') and _session.get('look')):
        return False
    prev = _session.get('prev')
    if prev is None or not _same_plane(design, prev, sk):
        return False
    _session['sk'] = sk.nativeObject or sk
    _session['ext'] = _extent(sk)
    _log('hop to "%s": same plane — camera kept' % sk.name)
    return True


def _exit(reason):
    """Leaving the sketch: back to the pre-sketch camera unless you orbited or
    stayed longer than STAY_KEEP_S."""
    global _session
    s, _session = _session, None
    if not s or not s.get('look'):
        return
    cam = _app.activeViewport.camera
    look_dir, look_up = s['look']
    d_dir = _angle_deg(cam.eye.vectorTo(cam.target), look_dir)
    d_up = _angle_deg(cam.upVector, look_up)
    now = time.time()
    stayed = now - s.get('since', now)
    if stayed > STAY_KEEP_S:
        _log('exit (%s): stayed %.0fs — kept' % (reason, stayed))
        return
    settling = now - s['look_at'] < ANIM_GRACE_S
    if settling or (d_dir < ORBIT_TOL_DEG and d_up < ORBIT_TOL_DEG):
        _put(s['pre'])
        _log('exit (%s): restored (dir %.2f°, up %.2f°)' % (reason, d_dir, d_up))
    else:
        _log('exit (%s): orbited — kept (dir %.2f°, up %.2f°)' % (reason, d_dir, d_up))


# ── handlers ──────────────────────────────────────────────────────────────────

class _OnStarting(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _entry_is_hop
        try:
            if args.commandId not in ENTRY_COMMANDS:
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            # Opening a sketch while one is open (or mid Edit Original hop) is a hop:
            # the camera to go back to is still the one from before the first.
            _entry_is_hop = bool(_session) and bool(design) and (
                fvs.is_editing_sketch(design) or fvs.master_hop_in_progress())
        except Exception:
            _log('starting:', traceback.format_exc().replace('\n', ' | '))


class _OnTerminated(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _session, _pending_cmd
        try:
            cmd = args.commandId
            if cmd == CMD_ID or not _enabled():
                return
            if cmd in ENTRY_COMMANDS:
                try:
                    done = (args.terminationReason ==
                            adsk.core.CommandTerminationReason.CompletedTerminationReason)
                except Exception:
                    done = True
                _log('%s terminated, reason=%s hop=%s' % (
                    cmd, args.terminationReason, _entry_is_hop))
                if not done:
                    return
                hop = bool(_entry_is_hop and _session)
                if not hop:
                    _session = {'pre': _grab(_app.activeViewport.camera),
                                'look': None, 'at': time.time(), 'look_at': 0.0,
                                'since': time.time()}
                _session['at'] = time.time()
                _session['hop'] = hop
                _session['prev'] = _session.get('sk') if hop else None
                _session['sk'] = _session['ext'] = None
                # Turn now if the sketch can be named already — first thing on
                # entry, not after every other add-in's entry work (seconds on a
                # big document). The deferred look below then only corrects it.
                design = adsk.fusion.Design.cast(_app.activeProduct)
                sk = _entered_now(design) if design else None
                if sk and _hop_keeps(design, sk):
                    pass
                elif sk:
                    _session['look'], _session['ext'] = _look_at(design, sk, now=True)
                    _session['sk'] = sk.nativeObject or sk
                    _session['look_at'] = time.time()
                # Deferred to after every other terminated handler: Go Down records
                # which sketch opened, Auto-Project Origin projects a new sketch's
                # bounds — both are what the framing reads.
                _pending_cmd = cmd
                threading.Thread(target=lambda: _app.fireCustomEvent(LOOK_EVENT, cmd),
                                 daemon=True).start()
                return
            if not _session:
                return
            if cmd == 'SketchStop':
                if not fvs.master_hop_in_progress():
                    _exit('SketchStop')
                return
            # Left some other way (a feature finishing the sketch, undo…).
            # SketchStop.isEnabled lags an entry by a beat, so only trust it later.
            if time.time() - _session['at'] > EXIT_FALLBACK_S:
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if design and not fvs.is_editing_sketch(design):
                    _exit(cmd)
        except Exception:
            _log('terminated:', traceback.format_exc().replace('\n', ' | '))


class _OnLook(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            if not _session or not _enabled():
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                return
            sk = _entered_sketch(design, args.additionalInfo or _pending_cmd)
            if not sk:
                _log('no sketch identified after', args.additionalInfo)
                return
            if (_session.get('sk') is not None and _session['sk'] == (sk.nativeObject or sk)
                    and _extent(sk) == _session.get('ext')):
                return                  # the entry handler's look still stands
            if _session.get('sk') is None and _hop_keeps(design, sk):
                return
            _session['look'], _session['ext'] = _look_at(design, sk)
            _session['look_at'] = time.time()
        except Exception:
            _log('look:', traceback.format_exc().replace('\n', ' | '))


class _OnDocActivated(adsk.core.DocumentEventHandler):
    def notify(self, args):
        global _session
        _session = None


class _ToggleExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _session
        try:
            on = not _enabled()
            _set_enabled(on)
            cd = _ui.commandDefinitions.itemById(CMD_ID)
            if cd:
                try:
                    cd.isChecked = on
                except Exception:
                    pass
            if on:
                _native_look_at_off()
            else:
                _session = None
            _log('toggled', 'ON' if on else 'OFF')
        except Exception:
            _ui.messageBox(traceback.format_exc())


class _ToggleCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = _ToggleExecute()
        args.command.execute.add(h)
        _handlers.append(h)


# ── lifecycle ─────────────────────────────────────────────────────────────────

def _panel():
    ws = _ui.workspaces.itemById('FusionSolidEnvironment')
    return ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        old = _ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()
        try:
            cd = _ui.commandDefinitions.addCheckBoxDefinition(
                CMD_ID, CMD_NAME, CMD_DESC, _enabled())
        except Exception:
            cd = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        h = _ToggleCreated()
        cd.commandCreated.add(h)
        _handlers.append(h)
        panel = _panel()
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
        panel.controls.addCommand(cd)

        h = _OnStarting()
        _ui.commandStarting.add(h)
        _handlers.append(h)
        h = _OnTerminated()
        _ui.commandTerminated.add(h)
        _handlers.append(h)

        _app.unregisterCustomEvent(LOOK_EVENT)
        ev = _app.registerCustomEvent(LOOK_EVENT)
        h = _OnLook()
        ev.add(h)
        _handlers.append(h)

        h = _OnDocActivated()
        _app.documentActivated.add(h)
        _handlers.append(h)

        if _enabled():
            _native_look_at_off()
        _log('=== run() — %s ===' % ('ON' if _enabled() else 'OFF'))
    except Exception:
        if _ui:
            _ui.messageBox('Sketch Look At start failed:\n' + traceback.format_exc())


def stop(context):
    global _session
    try:
        _session = None
        try:
            ctrl = _panel().controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        except Exception:
            pass
        cd = _ui.commandDefinitions.itemById(CMD_ID)
        if cd:
            cd.deleteMe()
        for h in _handlers:
            for ev in (_ui.commandStarting, _ui.commandTerminated, _app.documentActivated):
                try:
                    ev.remove(h)
                except Exception:
                    pass
        _handlers.clear()
        _app.unregisterCustomEvent(LOOK_EVENT)
        _log('=== stop() ===')
    except Exception:
        pass
