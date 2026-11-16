"""
Josh, Cursor Probe
==================
Runs a background thread that fires a Fusion custom event at ~20 Hz (and faster,
on demand). The handler executes on Fusion's main thread and:

  1. Reads the OS cursor position via ctypes + CoreGraphics/ObjC.
  2. Maps it into viewport pixel coords via the NSView subview tree.
  3. If the cursor moved, casts a ray with findBRepUsingRay.
  4. On a state change writes "1"/"0" to /tmp/fusion_geometry_under_cursor
     and fires btt://trigger_named/ (with -g to avoid stealing focus).
  5. Sets the orbit centre (synthesises ⌘I on geometry / ⌘U in free space) when
     BTT requests it (cursor dwell / finger lift).

It is also the in-Fusion half of the custom zoom system driven by the
FusionScrollFilter CGEventTap (see that file's header for the whole design).
The filter only sees scroll pixels; this add-in has camera/API access, so it:
  • writes the live eye→pivot and eye→origin distances (cm) that the filter
    reads to taper zoom (PIVOT_DIST / ORIGIN_DIST), recomputed even mid-gesture;
  • bumps ORBIT_GEN_FILE when the orbit centre is set on NEW geometry (the filter
    reads it to release its inner-wall zoom-in lock);
  • remembers the last geometry orbit centre (3D world point + screen px), kept
    through a small slip off the geometry (ORBIT_KEEP_PX);
  • executes "flings" the filter requests via FLING_FILE — an instant camera jump
    to a boundary (out → 5 m back, centred under the cursor; in → 3 cm from the
    orbit centre).
These extra IPC files live in /tmp; see the constants block below.
"""

import adsk.core
import adsk.fusion
import builtins
import ctypes
import math
import os
import platform
import threading
import time
import traceback
import urllib.request
import types as _types

# Shared visibility/sketch-state engine, loaded fresh from disk the same way every
# other add-in here loads it (a plain import can't reach a sibling add-in folder).
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fvs = _load('fvs', 'Visibility Snapshot.py')

# Vertical mapping policy: the viewport TOP is anchored to the window top (== the
# very top of the screen when maximized), assuming the canvas reaches that edge
# (top UI hidden). The ribbon/tabs are toggled often, so rather than calibrate
# per chrome state we accept a small offset on the rare occasions they're shown,
# in exchange for a mapping that never drifts and needs no calibration. See
# _get_viewport_quartz_origin. (Hiding the Design timeline lives below the canvas
# and never affects the top, so this is also immune to it — no add-in coupling.)

# ── ctypes bridges ────────────────────────────────────────────────────────────

class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]

class _CGSize(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]

class _CGRect(ctypes.Structure):
    _fields_ = [('origin', _CGPoint), ('size', _CGSize)]

try:
    _CG = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    _CG.CGEventCreate.restype       = ctypes.c_void_p
    _CG.CGEventCreate.argtypes      = [ctypes.c_void_p]
    _CG.CGEventGetLocation.restype  = _CGPoint
    _CG.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    _CG.CFRelease.argtypes          = [ctypes.c_void_p]
    _CG.CGMainDisplayID.restype     = ctypes.c_uint32
    _CG.CGDisplayPixelsHigh.restype  = ctypes.c_size_t
    _CG.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    # For synthesizing events (orbit center action)
    _CG.CGEventSourceCreate.restype      = ctypes.c_void_p
    _CG.CGEventSourceCreate.argtypes     = [ctypes.c_int]
    _CG.CGEventCreateKeyboardEvent.restype  = ctypes.c_void_p
    _CG.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    _CG.CGEventCreateMouseEvent.restype    = ctypes.c_void_p
    _CG.CGEventCreateMouseEvent.argtypes   = [ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
    _CG.CGEventPost.restype  = None
    _CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _CG.CGEventSetFlags.restype  = None
    _CG.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _CG.CGEventSourceSetUserData.restype  = None
    _CG.CGEventSourceSetUserData.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    _CG.CGEventSourceFlagsState.restype  = ctypes.c_uint64
    _CG.CGEventSourceFlagsState.argtypes = [ctypes.c_int]
    _CG.CGEventSourceButtonState.restype  = ctypes.c_bool
    _CG.CGEventSourceButtonState.argtypes = [ctypes.c_int, ctypes.c_uint32]
except Exception:
    _CG = None

try:
    _AX = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
    _AX.AXUIElementCreateSystemWide.restype  = ctypes.c_void_p
    _AX.AXUIElementCreateSystemWide.argtypes = []
    _AX.AXUIElementCopyElementAtPosition.restype  = ctypes.c_int32
    _AX.AXUIElementCopyElementAtPosition.argtypes = [
        ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _AX.AXUIElementGetPid.restype  = ctypes.c_int32
    _AX.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
    _AX.AXUIElementCopyAttributeValue.restype  = ctypes.c_int32
    _AX.AXUIElementCopyAttributeValue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
except Exception:
    _AX = None

# CoreFoundation — for reading the AXRole / AXSubrole CFStrings of the element
# under the cursor (so the orbit centre only fires over the 3D canvas, not a
# menu/dropdown floating over it).
try:
    _CF = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    _CF.CFStringCreateWithCString.restype  = ctypes.c_void_p
    _CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _CF.CFStringGetCString.restype  = ctypes.c_bool
    _CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _CF.CFRelease.argtypes = [ctypes.c_void_p]
    _kCFStringEncodingUTF8 = 0x08000100
except Exception:
    _CF = None

try:
    _OBJC = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    _OBJC.objc_getClass.restype     = ctypes.c_void_p
    _OBJC.objc_getClass.argtypes    = [ctypes.c_char_p]
    _OBJC.sel_registerName.restype  = ctypes.c_void_p
    _OBJC.sel_registerName.argtypes = [ctypes.c_char_p]
    _OBJC.objc_msgSend.restype      = ctypes.c_void_p
    _OBJC.objc_msgSend.argtypes     = [ctypes.c_void_p, ctypes.c_void_p]
    _IS_ARM64 = (platform.machine() == 'arm64')
    if not _IS_ARM64:
        _OBJC.objc_msgSend_stret.restype  = None
        _OBJC.objc_msgSend_stret.argtypes = [
            ctypes.POINTER(_CGRect), ctypes.c_void_p, ctypes.c_void_p
        ]
except Exception:
    _OBJC = None
    _IS_ARM64 = False

# ── Config ────────────────────────────────────────────────────────────────────

INTERVAL      = 0.05                          # 20 Hz background polling
DIST_INTERVAL = 0.03                          # ~33 Hz live pivot-distance updates during a gesture
OUTPUT        = '/tmp/fusion_geometry_under_cursor'
PIVOT_DIST    = '/tmp/fusion_pivot_distance'    # camera→geometry distance (cm); read by FusionScrollFilter
ORIGIN_DIST   = '/tmp/fusion_origin_distance'   # camera→model origin (0,0,0) distance (cm)
ZOOM_GAIN     = '/tmp/fusion_zoom_gain'         # pinch-zoom multiplier over empty space (§ Empty-space gain); 1 over geometry
ZOOM_GAIN_MIN, ZOOM_GAIN_MAX = 0.25, 20.0
FLING_FILE    = '/tmp/fusion_fling'             # FusionScrollFilter writes 'in'/'out' on a fast-swipe fling
ORBIT_GEN_FILE = '/tmp/fusion_orbit_gen'        # integer bumped each time the orbit centre is set on NEW geometry; filter reads it to release the inner-wall lock
ORBIT_GEN_EPS = 0.5                             # cm — orbit centre must move more than this to count as "new geometry"
ORBIT_KEEP_PX = 80                              # keep the geometry orbit centre if cursor slips this far off it (still allows in-fling)

# Fling landing distances (cm).
FLING_OUT_CM  = 500.0    # zoom-out fling: 5 m back from the orbit centre, centred on it
FLING_IN_CM   = 3.0      # zoom-in fling:  3 cm from the last orbit centre
PAUSE         = '/tmp/fusion_probe_pause'       # reanchor creates this while a touch begun over Fusion is down
REQUEST       = '/tmp/fusion_probe_request'     # BTT creates this to demand an immediate ray cast
ORBIT_REQUEST = '/tmp/fusion_orbit_center_request'  # fusion_dwell writes this to trigger orbit center
ORBIT_PAUSE   = '/tmp/fusion_orbit_pause'       # sticky manual pause: while present, the orbit centre is never set/reset. Created by orbit_pause.sh (BTT), cleared by orbit_resume.sh.
EVENT_ID      = 'Josh_CursorProbe_Poll'

# Virtual key codes (Carbon)
_VK_ANSI_I = 0x22   # "i" — Set Orbit Center on geometry
_VK_ANSI_U = 0x20   # "u" — Set Orbit Center (free space)

# Marker stamped on synthetic event sources so other CGEventTaps can identify
# and ignore our clicks (e.g. Josh, Suppress Plane Pick Grid).
_CURSOR_PROBE_MARKER = 0xCBEEF

# CGEventPost tap / source constants
_kCGHIDEventTap               = 0
_kCGEventSourceStateHIDSystemState = 1
_kCGEventLeftMouseDown        = 1
_kCGEventLeftMouseUp          = 2
_kCGMouseButtonLeft           = 0
_kCGMouseButtonCenter         = 2
_kCGEventFlagMaskCommand      = 0x00100000
_kCGEventFlagMaskShift        = 0x00020000

# ── State ─────────────────────────────────────────────────────────────────────

_app      = None
_ui       = None
_running  = False
_thread   = None
_handler  = None
_cmd_end_handler = None
_pending  = False        # True while a fired event hasn't been processed yet
_state    = None         # True / False / None (never written)
_last_cur = (None, None) # skip ray cast when cursor is stationary
_last_hit = None         # (x, y, z) cm of nearest geometry under cursor, or None
_last_orbit_center = None # (x, y, z) cm of last geometry-based orbit centre, or None
_last_orbit_screen = None # (vp_x, vp_y) px where the cursor was when orbit centre was set
_orbit_gen = 0            # bumped when the orbit centre is set on NEW geometry (≠ last by >ORBIT_GEN_EPS)


# ── Ray-casting ───────────────────────────────────────────────────────────────

def _ray_from_viewport(cam, vp_x, vp_y, vp_w, vp_h):
    eye    = cam.eye
    target = cam.target
    up_vec = cam.upVector

    fx = target.x - eye.x;  fy = target.y - eye.y;  fz = target.z - eye.z
    fl = math.sqrt(fx*fx + fy*fy + fz*fz)
    fx /= fl;  fy /= fl;  fz /= fl

    ux, uy, uz = up_vec.x, up_vec.y, up_vec.z
    rx = fy*uz - fz*uy;  ry = fz*ux - fx*uz;  rz = fx*uy - fy*ux
    rl = math.sqrt(rx*rx + ry*ry + rz*rz)
    rx /= rl;  ry /= rl;  rz /= rl

    tux = ry*fz - rz*fy;  tuy = rz*fx - rx*fz;  tuz = rx*fy - ry*fx

    nx =  (2.0 * vp_x / vp_w) - 1.0
    ny = -(2.0 * vp_y / vp_h) + 1.0

    if cam.cameraType != adsk.core.CameraTypes.OrthographicCameraType:
        th     = math.tan(cam.perspectiveAngle / 2.0)  # already in radians
        aspect = vp_w / vp_h
        dx = fx + rx * nx * aspect * th + tux * ny * th
        dy = fy + ry * nx * aspect * th + tuy * ny * th
        dz = fz + rz * nx * aspect * th + tuz * ny * th
        dl = math.sqrt(dx*dx + dy*dy + dz*dz)
        origin    = adsk.core.Point3D.create(eye.x, eye.y, eye.z)
        direction = adsk.core.Vector3D.create(dx/dl, dy/dl, dz/dl)
    else:
        half_h = cam.viewExtents / 2.0
        half_w = half_h * vp_w / vp_h
        ox = eye.x + rx * nx * half_w + tux * ny * half_h
        oy = eye.y + ry * nx * half_w + tuy * ny * half_h
        oz = eye.z + rz * nx * half_w + tuz * ny * half_h
        origin    = adsk.core.Point3D.create(ox, oy, oz)
        direction = adsk.core.Vector3D.create(fx, fy, fz)

    return origin, direction


def _find_sketch_profiles(app, vp, vp_x, vp_y):
    """Find the nearest sketch-profile interior under the cursor ray.

    Returns the (x, y, z) cm hit point on the nearest profile (so the caller can
    treat it exactly like a BRep hit — pivot distance, in-fling, ORBIT_GEN), or
    None if no profile is hit.
    """
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return None
        origin, direction = _ray_from_viewport(
            vp.camera, vp_x, vp_y, vp.width, vp.height
        )

        # Collect every sketch in the design in WORLD (assembly) context — sketches
        # live on components, which may sit under occurrences with their own
        # transforms, so we take the assembly-context proxy for each. The active
        # edit object comes first (it may not be enumerable elsewhere yet). Root
        # component sketches aren't under any occurrence, so add them directly.
        root = design.rootComponent
        seen = set()
        candidates = []

        # Dedupe key from cheap properties, NOT entityToken: every token read adds
        # permanent Entity-Tracking records to the document's undo history, and this
        # runs at 20 Hz over every sketch — it was ~3,500 records/s while hovering
        # empty canvas. A rare false duplicate only re-tests the same sketch.
        def _add(sk):
            if sk is None:
                return
            try:
                ctx = sk.assemblyContext
                tok = (sk.parentComponent.id, sk.name, ctx.fullPathName if ctx else '')
            except Exception:
                tok = id(sk)
            if tok in seen:
                return
            seen.add(tok)
            candidates.append((sk, tok))

        # fvs, not design.activeEditObject: the raw property raises for a sketch
        # nested in an occurrence and took this whole profile scan down with it.
        active = fvs.active_edit_sketch(design)
        if active:
            _add(active)
        active_tok = candidates[0][1] if candidates else None
        for occ in root.allOccurrences:
            try:
                for s in occ.component.sketches:
                    _add(s.createForAssemblyContext(occ))
            except Exception:
                pass
        for s in root.sketches:
            _add(s)

        # Keep the profile hit nearest the eye, matching the BRep nearest-hit rule.
        nearest, nearest_d2 = None, None
        for sketch, tok in candidates:
            hit = _sketch_has_ray(sketch, origin, direction, tok, tok == active_tok)
            if hit is None:
                continue
            dx = hit[0] - origin.x; dy = hit[1] - origin.y; dz = hit[2] - origin.z
            d2 = dx*dx + dy*dy + dz*dz
            if nearest_d2 is None or d2 < nearest_d2:
                nearest, nearest_d2 = hit, d2
        return nearest
    except Exception:
        return None


# ── Sketch profile cache ──
# sketch.profiles is recomputed on every read: 2.6-3.6 s on 260612's Section Sketch
# sketch (278 curves, 0 profiles — 2026-10-06), read here at 20 Hz whenever the cursor
# moved off a solid. The sketch froze in every pan. So each sketch's loops are kept
# as 2D points and re-read only after a command ends (_gen, _OnCommandEnd). A sketch
# that took over PROFILE_SLOW_S to read is kept until its curve/point counts change,
# and never re-read while it is the sketch being edited — a stale loop only moves
# where a pivot lands; another multi-second read after every line drawn is worse.
_PROFILES = {}      # sketch key -> (gen, counts, slow, [[(is_outer, [pts per curve])]])
_gen = 0
PROFILE_SLOW_S = 0.03   # 0.3 let a 243 ms read through, re-read after every gesture's
                        # orbit-centre set — a stall at each nav start/switch (2026-10-06)


def _profile_loops(sketch, key, active):
    ent = _PROFILES.get(key)
    if ent is not None and ent[0] == _gen:
        return ent[3]
    counts = None
    if ent is not None and ent[2]:
        if active:
            return ent[3]
        try:
            counts = (sketch.sketchCurves.count, sketch.sketchPoints.count)
        except Exception:
            return ent[3]
        if counts == ent[1]:
            _PROFILES[key] = (_gen,) + ent[1:]
            return ent[3]
    t0 = time.time()
    profiles = []
    try:
        if counts is None:
            counts = (sketch.sketchCurves.count, sketch.sketchPoints.count)
        for profile in sketch.profiles:
            profiles.append([(loop.isOuter, [_curve_to_2d_pts(pc.geometry)
                                             for pc in loop.profileCurves])
                             for loop in profile.profileLoops])
    except Exception:
        pass
    if key is not None:
        if len(_PROFILES) > 500:
            _PROFILES.clear()
        _PROFILES[key] = (_gen, counts, time.time() - t0 > PROFILE_SLOW_S, profiles)
    return profiles


class _OnCommandEnd(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _gen
        _gen += 1
        # Set Orbit Center stays active from ⌘I until the click picks: its end is
        # the click landing, so the slice goes unselectable before any hover can
        # highlight it.
        try:
            if _slice_unpick_at and args.commandId == 'SetOrbitCenterCommand':
                _slice_pickable(False)
        except Exception:
            pass


def _sketch_has_ray(sketch, origin, direction, key=None, active=False):
    """Return the world (x, y, z) cm point where the ray hits a profile interior in
    this sketch, or None if it misses every profile.

    The ray, plane and returned hit are all in world space. Profile curve geometry,
    however, is reported in the sketch's *own* 2D space (z≈0), so we intersect the
    ray with the sketch plane in world, transform that hit back into sketch space
    via the inverse sketch transform, and run the point-in-polygon test there —
    where the sketch (x, y) IS the 2D coordinate system. This works for a sketch on
    any plane / occurrence, not just one coincident with a world origin plane.
    """
    try:
        if not sketch.isVisible:
            return None
        profiles = _profile_loops(sketch, key, active)
        if not profiles:
            return None

        # World-space sketch frame (proxy axes/origin are already in world space).
        xd = sketch.xDirection
        yd = sketch.yDirection
        normal = xd.crossProduct(yd)
        normal.normalize()
        plane_pt = sketch.origin
        to_sketch = sketch.transform.copy()  # sketch-space → world
        if not to_sketch.invert():           # → world → sketch-space
            return None
    except Exception:
        return None

    # Ray–plane intersection (world space)
    nd = normal.x*direction.x + normal.y*direction.y + normal.z*direction.z
    if abs(nd) < 1e-9:
        return None
    t = ((plane_pt.x - origin.x)*normal.x +
         (plane_pt.y - origin.y)*normal.y +
         (plane_pt.z - origin.z)*normal.z) / nd
    if t < 0.0:
        return None

    hit = adsk.core.Point3D.create(
        origin.x + t*direction.x,
        origin.y + t*direction.y,
        origin.z + t*direction.z,
    )

    # Drop the world hit into sketch space; its (x, y) are the 2D test coordinates.
    hs = hit.copy()
    hs.transformBy(to_sketch)
    hu, hv = hs.x, hs.y

    for loops in profiles:
        if _profile_contains(loops, hu, hv):
            return (hit.x, hit.y, hit.z)
    return None


def _profile_contains(loops, hu, hv):
    """True if sketch-space 2D point (hu, hv) is inside the profile region whose
    loops (_profile_loops) are given."""
    in_outer = False
    for is_outer, curves in loops:
        inside = _loop_encloses(curves, hu, hv)
        if is_outer:
            if not inside:
                return False
            in_outer = True
        elif inside:
            return False  # point is inside a hole
    return in_outer


def _curve_to_2d_pts(geom):
    """Sample a sketch-space Curve3D and return a list of (x, y) points.

    Profile curve geometry lives in the sketch's own 2D plane (z≈0), so the
    sketch (x, y) of each sampled point is already the 2D coordinate — no
    projection needed. Line3D has no .evaluator in the Fusion API, so read its
    endpoints directly; everything else (Arc3D, NurbsCurve3D …) is sampled
    through the CurveEvaluator3D interface.
    """
    pts = []
    try:
        if hasattr(geom, 'startPoint') and hasattr(geom, 'endPoint') \
                and not hasattr(geom, 'evaluator'):
            # Line3D: two endpoints are all that's needed
            pts.append((geom.startPoint.x, geom.startPoint.y))
            pts.append((geom.endPoint.x, geom.endPoint.y))
        else:
            ev = geom.evaluator
            _, t0, t1 = ev.getParameterExtents()
            for i in range(9):
                param = t0 + (t1 - t0) * i / 8.0
                ok, pt3 = ev.getPointAtParameter(param)
                if ok:
                    pts.append((pt3.x, pt3.y))
            # Ensure start/end are included even if the evaluator skips them
            if not pts and hasattr(geom, 'startPoint'):
                pts.append((geom.startPoint.x, geom.startPoint.y))
                pts.append((geom.endPoint.x, geom.endPoint.y))
    except Exception:
        # Last-resort fallback: if we can get startPoint/endPoint, use them
        try:
            pts.append((geom.startPoint.x, geom.startPoint.y))
            pts.append((geom.endPoint.x, geom.endPoint.y))
        except Exception:
            pass
    return pts


def _loop_encloses(curves, hu, hv):
    """Ray-casting point-in-polygon test over a loop's curves, each sampled to 2D
    points (sketch space)."""
    crossings = 0
    for pts in curves:
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i+1]
            if (ay <= hv < by) or (by <= hv < ay):
                x_cross = ax + (bx - ax) * (hv - ay) / (by - ay)
                if hu < x_cross:
                    crossings += 1
    return (crossings % 2) == 1


def _find_geometry(app, vp, vp_x, vp_y):
    """Return True / False / None(error) - checks both BRep faces and sketch profiles.

    Side effect: updates the module global _last_hit with the (x, y, z) cm of the
    nearest BRep hit point under the cursor (the point the orbit centre lands on),
    or None when no solid geometry is hit. FusionScrollFilter uses the live
    eye→_last_hit distance to taper zoom speed near geometry.
    """
    global _last_hit, _slice_hit
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return None
        origin, direction = _ray_from_viewport(
            vp.camera, vp_x, vp_y, vp.width, vp.height
        )

        # Check for BRep faces
        hit_points = adsk.core.ObjectCollection.create()
        brep_results = design.rootComponent.findBRepUsingRay(
            origin, direction,
            adsk.fusion.BRepEntityTypes.BRepFaceEntityType,
            -1, True, hit_points,
        )
        brep_found = bool(brep_results and brep_results.count > 0 and hit_points.count > 0)

        # Section Sketch's slice: custom graphics, which no API ray cast sees.
        slice_hit = _slice_ray(origin, direction)
        if brep_found:
            # Record the hit nearest the eye as the pivot-distance reference.
            nearest, nearest_d2 = None, None
            for i in range(hit_points.count):
                p  = hit_points.item(i)
                dx = p.x - origin.x; dy = p.y - origin.y; dz = p.z - origin.z
                d2 = dx*dx + dy*dy + dz*dz
                if nearest_d2 is None or d2 < nearest_d2:
                    nearest, nearest_d2 = p, d2
            _last_hit = (nearest.x, nearest.y, nearest.z) if nearest else None
            _slice_hit = slice_hit is not None and nearest_d2 is not None \
                and slice_hit[0] * slice_hit[0] < nearest_d2
            if _slice_hit:
                _last_hit = slice_hit[1]
            return True
        if slice_hit is not None:
            _last_hit, _slice_hit = slice_hit[1], True
            return True
        _slice_hit = False

        # No solid under the cursor — check for sketch profiles and treat a hit
        # as geometry: record the profile hit point as _last_hit so the pivot
        # distance taper, in-fling memory and ORBIT_GEN lock all behave exactly
        # as they do for a BRep face. None ⇒ nothing under the cursor.
        profile_hit = _find_sketch_profiles(app, vp, vp_x, vp_y)
        _last_hit = profile_hit
        return profile_hit is not None
    except Exception:
        return None


# ── Section Sketch slice ──────────────────────────────────────────────────────
# In a section sketch the bodies are drawn by Section.py as custom graphics (the
# live ones are rolled back or held off), which findBRepUsingRay can't see and
# Set Orbit Center can't pick while they're unselectable. So the slice's meshes
# are ray-tested here, read back once per drawn group (~40 ms for 15k triangles
# on 260612), and for the synthetic ⌘I click a tiny transparent pick target is
# put at the hit point (_slice_pickable) — the slice stays unselectable, so
# sketch clicks never pick it. Section's state is shared through builtins
# (Section.py STATE).

SLICE_ID     = 'SectionSketchSlice'
SLICE_CHUNK  = 64       # triangles per bounding box inside one mesh
PICK_ID      = 'CursorProbePick'
PICK_PX      = 2.5      # pick target half-size, screen px
PICK_LIFT    = 0.003    # pulled toward the eye by this share of its distance (slice bias: 0.001)
SLICE_PICK_S = 0.3      # the pick target is removed this long after the click at the latest
                        # (normally sooner: when Set Orbit Center ends, _OnCommandEnd)
_slice_cache = (None, [])   # (group, [(box, [(box, [(v0, e1, e2)…])…])…])
_slice_hit   = False        # the last _find_geometry hit was the slice
_slice_unpick_at = 0.0      # when the pick target goes (0: there is none)


def _slice_group():
    st = builtins.__dict__.get('_fusion_section')
    if not st or not st.get('slice_shown'):
        return None
    g = st.get('slice_group')
    try:
        if g is None or not g.isValid:   # _bias_slice failed: find it by id
            g = None
            groups = _app.activeProduct.rootComponent.customGraphicsGroups
            for i in range(groups.count):
                if groups.item(i).id == SLICE_ID:
                    g = groups.item(i)
                    break
        return g if g is not None and g.isVisible else None
    except Exception:
        return None


def _box_of(pts):
    xs, ys, zs = pts[0::3], pts[1::3], pts[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _slice_meshes(g):
    global _slice_cache
    if _slice_cache[0] is g:
        return _slice_cache[1]
    meshes = []
    for i in range(g.count):
        m = adsk.fusion.CustomGraphicsMesh.cast(g.item(i))
        if m is None:
            continue
        P, I = m.coordinates.coordinates, m.vertexIndexList
        chunks = []
        for c in range(0, len(I) - 2, 3 * SLICE_CHUNK):
            tris, pts = [], []
            for t in range(c, min(c + 3 * SLICE_CHUNK, len(I) - 2), 3):
                a, b, d = 3 * I[t], 3 * I[t + 1], 3 * I[t + 2]
                v0 = (P[a], P[a + 1], P[a + 2])
                tris.append((v0, (P[b] - v0[0], P[b + 1] - v0[1], P[b + 2] - v0[2]),
                                 (P[d] - v0[0], P[d + 1] - v0[1], P[d + 2] - v0[2])))
                pts += P[a:a + 3] + P[b:b + 3] + P[d:d + 3]
            chunks.append((_box_of(pts), tris))
        if chunks:
            lo = tuple(min(ch[0][0][k] for ch in chunks) for k in range(3))
            hi = tuple(max(ch[0][1][k] for ch in chunks) for k in range(3))
            meshes.append(((lo, hi), chunks))
    _slice_cache = (g, meshes)
    return meshes


def _ray_box(o, inv, box, tmax):
    t0, t1 = 0.0, tmax
    for k in range(3):
        lo, hi = box[0][k], box[1][k]
        if inv[k] is None:
            if o[k] < lo or o[k] > hi:
                return False
            continue
        ta, tb = (lo - o[k]) * inv[k], (hi - o[k]) * inv[k]
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return False
    return True


def _slice_ray(origin, direction):
    """(distance, (x, y, z) cm) of the nearest slice triangle on the ray, or None.
    The group's bias transform (a 0.1 % pull toward the eye) is ignored."""
    g = _slice_group()
    if g is None:
        return None
    try:
        meshes = _slice_meshes(g)
    except Exception:
        return None
    o = (origin.x, origin.y, origin.z)
    d = (direction.x, direction.y, direction.z)
    inv = tuple(1.0 / c if abs(c) > 1e-12 else None for c in d)
    best = float('inf')
    for box, chunks in meshes:
        if not _ray_box(o, inv, box, best):
            continue
        for cbox, tris in chunks:
            if not _ray_box(o, inv, cbox, best):
                continue
            for v0, e1, e2 in tris:     # Möller–Trumbore, both sides
                px = d[1] * e2[2] - d[2] * e2[1]
                py = d[2] * e2[0] - d[0] * e2[2]
                pz = d[0] * e2[1] - d[1] * e2[0]
                det = e1[0] * px + e1[1] * py + e1[2] * pz
                if -1e-12 < det < 1e-12:
                    continue
                f = 1.0 / det
                tx, ty, tz = o[0] - v0[0], o[1] - v0[1], o[2] - v0[2]
                u = (tx * px + ty * py + tz * pz) * f
                if u < 0.0 or u > 1.0:
                    continue
                qx = ty * e1[2] - tz * e1[1]
                qy = tz * e1[0] - tx * e1[2]
                qz = tx * e1[1] - ty * e1[0]
                v = (d[0] * qx + d[1] * qy + d[2] * qz) * f
                if v < 0.0 or u + v > 1.0:
                    continue
                t = (e2[0] * qx + e2[1] * qy + e2[2] * qz) * f
                if 1e-6 < t < best:
                    best = t
    if best == float('inf'):
        return None
    return best, (o[0] + d[0] * best, o[1] + d[1] * best, o[2] + d[2] * best)


def _slice_pickable(on):
    """Put a pick target for the orbit click under the cursor (on), or remove it.

    The target is a few-pixel transparent square at the slice hit point (_last_hit),
    facing the camera and nudged toward the eye so it is in front of the slice.
    Making the slice itself selectable instead flashed the whole body's highlight."""
    global _slice_unpick_at
    _slice_unpick_at = time.time() + SLICE_PICK_S if on else 0.0
    try:
        root = _app.activeProduct.rootComponent
        groups = root.customGraphicsGroups
        for i in range(groups.count - 1, -1, -1):
            if groups.item(i).id == PICK_ID:
                groups.item(i).deleteMe()
        if not on or _last_hit is None:
            return
        vp = _app.activeViewport
        cam = vp.camera
        e, t = cam.eye, cam.target
        f = (t.x - e.x, t.y - e.y, t.z - e.z)
        fl = math.sqrt(f[0]*f[0] + f[1]*f[1] + f[2]*f[2])
        f = (f[0]/fl, f[1]/fl, f[2]/fl)
        u = cam.upVector
        r = (f[1]*u.z - f[2]*u.y, f[2]*u.x - f[0]*u.z, f[0]*u.y - f[1]*u.x)
        rl = math.sqrt(r[0]*r[0] + r[1]*r[1] + r[2]*r[2])
        r = (r[0]/rl, r[1]/rl, r[2]/rl)
        up = (r[1]*f[2] - r[2]*f[1], r[2]*f[0] - r[0]*f[2], r[0]*f[1] - r[1]*f[0])
        h = _last_hit
        dist = math.sqrt((h[0]-e.x)**2 + (h[1]-e.y)**2 + (h[2]-e.z)**2)
        if cam.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
            px = cam.viewExtents / vp.height
            c = tuple(h[k] - f[k] * PICK_LIFT * dist for k in range(3))
        else:
            px = 2.0 * dist * math.tan(cam.perspectiveAngle / 2.0) / vp.height
            s = 1.0 - PICK_LIFT              # along the eye→hit ray: same screen spot
            c = (e.x + (h[0]-e.x)*s, e.y + (h[1]-e.y)*s, e.z + (h[2]-e.z)*s)
            px *= s
        a = PICK_PX * px
        pts = []
        for sr, su in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            pts += [c[k] + sr*a*r[k] + su*a*up[k] for k in range(3)]
        g = groups.add()
        g.id = PICK_ID
        m = g.addMesh(adsk.fusion.CustomGraphicsCoordinates.create(pts),
                      [0, 1, 2, 0, 2, 3], [], [])
        m.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(0, 0, 0, 0))
    except Exception:
        pass


# ── ObjC helpers ──────────────────────────────────────────────────────────────

def _o_ptr(obj, sel_b):
    f = _OBJC.objc_msgSend
    f.restype  = ctypes.c_void_p
    f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return f(obj, _OBJC.sel_registerName(sel_b))

def _o_ulong(obj, sel_b):
    f = _OBJC.objc_msgSend
    f.restype  = ctypes.c_ulong
    f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return f(obj, _OBJC.sel_registerName(sel_b))

def _o_ptr_idx(obj, sel_b, idx):
    f = _OBJC.objc_msgSend
    f.restype  = ctypes.c_void_p
    f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    return f(obj, _OBJC.sel_registerName(sel_b), ctypes.c_ulong(idx))

def _o_rect(obj, sel_b):
    sel = _OBJC.sel_registerName(sel_b)
    if _IS_ARM64:
        f          = _OBJC.objc_msgSend
        f.restype  = _CGRect
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return f(obj, sel)
    else:
        r          = _CGRect()
        f          = _OBJC.objc_msgSend_stret
        f.restype  = None
        f.argtypes = [ctypes.POINTER(_CGRect), ctypes.c_void_p, ctypes.c_void_p]
        f(ctypes.byref(r), obj, sel)
        return r


# ── Viewport origin cache ─────────────────────────────────────────────────────

_vp_origin_cache    = None
_vp_origin_win_size = None   # (win_w, win_h, vp_height) — busts on chrome toggle


def _find_subview_with_size(view, target_w, target_h, acc_x, acc_y_ak, depth=0):
    if depth > 10:
        return None
    try:
        b = _o_rect(view, b'bounds')
        if abs(b.size.width - target_w) <= 4 and abs(b.size.height - target_h) <= 4:
            return acc_x, acc_y_ak
        subviews = _o_ptr(view, b'subviews')
        if not subviews:
            return None
        count = int(_o_ulong(subviews, b'count'))
        for i in range(min(count, 40)):
            sv = _o_ptr_idx(subviews, b'objectAtIndex:', i)
            if not sv:
                continue
            fr = _o_rect(sv, b'frame')
            result = _find_subview_with_size(
                sv, target_w, target_h,
                acc_x + fr.origin.x,
                acc_y_ak + fr.origin.y,
                depth + 1,
            )
            if result is not None:
                return result
    except Exception:
        pass
    return None


def _get_viewport_quartz_origin(vp):
    global _vp_origin_cache, _vp_origin_win_size
    if not _OBJC or not _CG:
        return None
    try:
        NSApp_cls = _OBJC.objc_getClass(b'NSApplication')
        ns_app    = _o_ptr(NSApp_cls, b'sharedApplication')
        if not ns_app:
            return None

        wins  = _o_ptr(ns_app, b'windows')
        count = int(_o_ulong(wins, b'count'))
        best_win, best_area, best_frame = None, 0, None
        for i in range(count):
            w = _o_ptr_idx(wins, b'objectAtIndex:', i)
            if not w:
                continue
            fr   = _o_rect(w, b'frame')
            area = fr.size.width * fr.size.height
            if area > best_area:
                best_win, best_area, best_frame = w, area, fr

        if not best_win:
            return None

        win_w = best_frame.size.width
        win_h = best_frame.size.height

        # vp.height is in the cache key: toggling Minimal UI changes it and
        # busts the cache (window size alone does not change on toggle).
        key = (win_w, win_h, round(vp.height))
        if _vp_origin_win_size != key:
            _vp_origin_cache    = None
            _vp_origin_win_size = key

        if _vp_origin_cache is not None:
            return _vp_origin_cache

        content = _o_ptr(best_win, b'contentView')
        result  = _find_subview_with_size(content, vp.width, vp.height, 0.0, 0.0)

        screen_h = _CG.CGDisplayPixelsHigh(_CG.CGMainDisplayID())
        win_ak_y = best_frame.origin.y

        # Anchor the viewport TOP to the window top — which is the very top of the
        # screen when the window is maximized. We deliberately assume the canvas
        # reaches that top edge (i.e. the top UI is hidden). The ribbon/tabs are
        # toggled often; rather than calibrate per chrome state, we accept a small
        # vertical offset on the rare occasions they're shown, in exchange for a
        # dead-simple mapping that never drifts and needs no calibration. The
        # Design timeline lives below the canvas, so hiding it never affects this.
        qy = screen_h - (win_ak_y + win_h)
        # Horizontal origin from the matched viewport NSView if found (handles a
        # left-docked browser panel); else the window's left edge.
        qx = best_frame.origin.x + (result[0] if result is not None else 0.0)

        _vp_origin_cache = (qx, qy)
        return _vp_origin_cache
    except Exception:
        return None


# ── Mouse → viewport coords ───────────────────────────────────────────────────

def _mouse_viewport_pos(vp):
    try:
        if not _CG:
            return None, None
        ev  = _CG.CGEventCreate(None)
        loc = _CG.CGEventGetLocation(ev)
        _CG.CFRelease(ev)
        origin = _get_viewport_quartz_origin(vp)
        if origin is None:
            return None, None

        vp_x = int(loc.x - origin[0])
        vp_y = int(loc.y - origin[1])

        if vp_x < 0 or vp_y < 0 or vp_x >= vp.width or vp_y >= vp.height:
            return None, None
        return vp_x, vp_y
    except Exception:
        return None, None


# ── Focus / window guard ─────────────────────────────────────────────────────

_ax_system_element = None   # AXUIElementCreateSystemWide() result, created once
_fusion_pid        = os.getpid()  # PID of this process = Fusion's PID


def _ax_str_attr(elem, attr_name):
    """Read a string AX attribute (e.g. 'AXRole') of `elem`, or None."""
    if not _AX or not _CF:
        return None
    cf_attr = _CF.CFStringCreateWithCString(
        None, attr_name.encode('utf-8'), _kCFStringEncodingUTF8)
    if not cf_attr:
        return None
    try:
        out = ctypes.c_void_p(0)
        err = _AX.AXUIElementCopyAttributeValue(elem, cf_attr, ctypes.byref(out))
        if err != 0 or not out.value:
            return None
        buf = ctypes.create_string_buffer(256)
        ok  = _CF.CFStringGetCString(out.value, buf, 256, _kCFStringEncodingUTF8)
        _CF.CFRelease(out.value)
        return buf.value.decode('utf-8') if ok else None
    finally:
        _CF.CFRelease(cf_attr)


def _ax_bool_attr(elem, attr_name):
    """Return True/False for an AX element's boolean attribute (e.g. 'AXMain'),
    or None if absent/unreadable. AXMain is how we identify the canvas (the app's
    MAIN window) vs any dialog/panel (AXMain=false even when large)."""
    if not _AX or not _CF:
        return None
    cf_attr = _CF.CFStringCreateWithCString(
        None, attr_name.encode('utf-8'), _kCFStringEncodingUTF8)
    if not cf_attr:
        return None
    try:
        out = ctypes.c_void_p(0)
        if _AX.AXUIElementCopyAttributeValue(elem, cf_attr, ctypes.byref(out)) != 0 or not out.value:
            return None
        try:
            _CF.CFBooleanGetValue.restype  = ctypes.c_ubyte
            _CF.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
            return bool(_CF.CFBooleanGetValue(out.value))
        finally:
            _CF.CFRelease(out.value)
    finally:
        _CF.CFRelease(cf_attr)


def _ax_window_size(elem):
    """Return (w, h) of an AX element's AXSize, or None. Fallback used only when
    AXMain is unreadable, to tell the (display-filling) borderless canvas from a
    (smaller) dialog."""
    if not _AX or not _CF:
        return None
    try:
        _AX.AXValueGetValue.restype  = ctypes.c_bool
        _AX.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
        cf_attr = _CF.CFStringCreateWithCString(None, b'AXSize', _kCFStringEncodingUTF8)
        if not cf_attr:
            return None
        try:
            out = ctypes.c_void_p(0)
            if _AX.AXUIElementCopyAttributeValue(elem, cf_attr, ctypes.byref(out)) != 0 or not out.value:
                return None
            sz = _CGSize()
            ok = _AX.AXValueGetValue(out.value, 2, ctypes.byref(sz))  # 2 = kAXValueCGSizeType
            _CF.CFRelease(out.value)
            return (sz.width, sz.height) if ok else None
        finally:
            _CF.CFRelease(cf_attr)
    except Exception:
        return None


# ── Qt widget-under-cursor gate ───────────────────────────────────────────────
# Fusion's accessibility tree is opaque: every point inside the main window
# reports that window and nothing deeper, and Qt popups (the S-menu search
# completer, context menus) are native windows AX does not list at all. So the
# AX gate below can only reject foreign apps and Fusion's separate palettes /
# dialogs — anything Fusion draws INSIDE the main window (the S-menu dropdown,
# a toolbar, the timeline) looks exactly like the canvas to it, and the orbit
# centre's synthetic ⌘I + click lands on whatever is under the cursor (picking
# the menu item the cursor happens to be over, pressing a button, …).
#
# The same blindness applies to keyboard focus: _has_text_focus below only knows
# AppKit text views, and every Fusion text box (the dimension HUD, a command
# dialog field, the S-menu search box) is a Qt widget inside one opaque NSView —
# so typing a dimension never looked like "text has focus" either, and a dwell
# mid-typing fired ⌘I + a click into the model.
#
# Qt knows precisely what is under the cursor and what has focus, so ask it:
#   • QApplication::activePopupWidget() — any popup open (the S-menu completer
#     list, a context menu) means the click would go to the popup → suppress;
#   • QApplication::widgetAt(cursor) must be the 3D canvas widget (QTCanvas);
#   • QApplication::focusWidget() must not be a text-entry widget.
# These are plain reads on Fusion's main thread (they only run from the custom
# event handler), and a binding failure fails open so a Fusion update that moves
# these symbols degrades to the old AX-only behaviour instead of breaking.

CANVAS_CLASS = 'QTCanvas'   # Qt class of Fusion's 3D viewport widget

# Qt::WA_InputMethodEnabled — set on every widget that accepts typed text. It is
# below 32, so it lives in the inline attribute word of QWidgetData rather than
# behind testAttribute_helper() (that helper only covers attributes >= 32 and
# would index out of bounds if asked for this one).
WA_INPUT_METHOD_ENABLED = 14
# Class-name fallback, in case a custom Fusion editor leaves the attribute off.
TEXT_INPUT_HINTS = ('edit', 'spinbox', 'combobox', 'searchbox', 'textfield')
# Item views (QHeaderView, QListView, …) also set WA_InputMethodEnabled — for
# type-to-search, not text entry — and one of them holding focus must not kill
# the orbit centre. (The S-menu completer is a QListView; it stays blocked by
# activePopupWidget, which is the honest reason to block it anyway.)
NOT_TEXT_INPUT_SUFFIX = 'View'

FOCUS_LOG = '/tmp/fusion_probe_focus'   # last focus-widget change (diagnostics)

_qt_bind = None   # _QtBind once bound, False once binding has failed


class _QPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32)]


def _dyld_image(substr):
    """Path of the loaded Mach-O image whose name contains `substr`, or None."""
    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype  = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    needle = substr.encode()
    for i in range(libc._dyld_image_count()):
        name = libc._dyld_get_image_name(i)
        if name and needle in name:
            return name.decode()
    return None


class _QtBind:
    """The QApplication entry points we need, bound by mangled symbol."""

    def __init__(self):
        qtw = ctypes.CDLL(_dyld_image('QtWidgets.framework/QtWidgets'))
        qtc = ctypes.CDLL(_dyld_image('QtCore.framework/QtCore'))
        self._widget_at = qtw['_ZN12QApplication8widgetAtERK6QPoint']
        self._widget_at.restype  = ctypes.c_void_p
        self._widget_at.argtypes = [ctypes.POINTER(_QPoint)]
        self._active_popup = qtw['_ZN12QApplication17activePopupWidgetEv']
        self._active_popup.restype  = ctypes.c_void_p
        self._active_popup.argtypes = []
        self._focus_widget = qtw['_ZN12QApplication11focusWidgetEv']
        self._focus_widget.restype  = ctypes.c_void_p
        self._focus_widget.argtypes = []
        self._meta_class_name = qtc['_ZNK11QMetaObject9classNameEv']
        self._meta_class_name.restype  = ctypes.c_char_p
        self._meta_class_name.argtypes = [ctypes.c_void_p]

    def popup_open(self):
        return bool(self._active_popup())

    def cls(self, w):
        """Qt class name of a QObject."""
        # QObject::metaObject() is the first virtual slot — the same read
        # Maximize Viewport does.
        vt = ctypes.c_void_p.from_address(w).value
        fn = ctypes.c_void_p.from_address(vt).value
        mo = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(fn)(w)
        return (self._meta_class_name(mo) or b'').decode('utf-8', 'replace')

    def attributes(self, w):
        """QWidgetData::widget_attributes — the low 32 Qt::WidgetAttribute bits.

        QWidget holds its QWidgetData pointer at +0x20, and the struct starts
        WId winid (8 bytes) then this word; the same layout Maximize Viewport
        relies on to read crect out of it.
        """
        data = ctypes.c_void_p.from_address(w + 0x20).value
        return ctypes.c_uint32.from_address(data + 8).value if data else 0

    def focus(self):
        """(class name, attribute word) of the focused widget, or (None, 0).

        Null while Fusion is not the active application — the case
        _has_text_focus already refuses on.
        """
        w = self._focus_widget()
        return (self.cls(w), self.attributes(w)) if w else (None, 0)

    def class_at(self, x, y):
        """Qt class name of the widget at global (x, y); '' when there is none."""
        p = _QPoint(int(x), int(y))
        w = self._widget_at(ctypes.byref(p))
        return self.cls(w) if w else ''


def _qt():
    """The lazily-bound Qt entry points, or None if they can't be bound."""
    global _qt_bind
    if _qt_bind is None:
        try:
            _qt_bind = _QtBind()
        except Exception:
            _qt_bind = False
    return _qt_bind or None


def _text_input_focused():
    """
    True when a Qt text-entry widget holds keyboard focus — the sketch dimension
    HUD, a command-dialog field, the S-menu search box, a rename field.

    Fails False (orbit allowed) on any error, like the rest of these gates.
    """
    try:
        qt = _qt()
        if qt is None:
            return False
        cls_name, attrs = qt.focus()
        if not cls_name or cls_name == CANVAS_CLASS:
            return False
        if cls_name.endswith(NOT_TEXT_INPUT_SUFFIX):
            return False
        if attrs & (1 << WA_INPUT_METHOD_ENABLED):
            return True
        low = cls_name.lower()
        return any(hint in low for hint in TEXT_INPUT_HINTS)
    except Exception:
        return False


# A dwell request is written by fusion_dwell and only handled on the next tick
# (<=50 ms later), and typing Enter in a dimension box — like clicking a menu
# item — dismisses the very UI that should have vetoed it, leaving bare canvas
# under the cursor by the time the request is read. So every tick stamps the
# last moment the UI said "not now", and a request is dropped while that stamp
# is fresh. While a text box keeps focus the stamp is refreshed continuously, so
# this is a live veto for that whole time, not just a trailing grace period.
UI_GRACE_SECS = 0.30

_last_block_ts = -1.0     # time.monotonic() of the last tick that vetoed firing
_last_focus_cls = None    # last focused-widget class seen (for FOCUS_LOG)


def _note_ui_context():
    """Tick sampler: stamp the time whenever the UI state forbids firing."""
    global _last_block_ts, _last_focus_cls
    typing = _text_input_focused()
    if typing or not _cursor_over_canvas_widget():
        _last_block_ts = time.monotonic()
    # Diagnostics: append focus CHANGES only (a handful of lines per command), so
    # a "why did/didn't it fire" question can be answered after the fact.
    try:
        qt = _qt()
        cls_name = qt.focus()[0] if qt else None
        if cls_name != _last_focus_cls:
            _last_focus_cls = cls_name
            if os.path.exists(FOCUS_LOG) and os.path.getsize(FOCUS_LOG) > 200000:
                os.remove(FOCUS_LOG)
            with open(FOCUS_LOG, 'a') as f:
                f.write('%s  focus=%s text_input=%s\n'
                        % (time.strftime('%H:%M:%S'), cls_name, typing))
    except Exception:
        pass


def _ui_blocked_recently():
    """True if a tick vetoed firing within the grace window."""
    return (time.monotonic() - _last_block_ts) < UI_GRACE_SECS


def _cursor_over_canvas_widget():
    """
    True when Qt reports the 3D canvas under the cursor and no popup is open.

    Fails OPEN (True) on any binding/read error, so this can only ever add
    rejections the AX gate already intends — never block normal orbit use.
    """
    try:
        qt = _qt()
        if qt is None or not _CG:
            return True
        if qt.popup_open():
            return False
        ev  = _CG.CGEventCreate(None)
        cur = _CG.CGEventGetLocation(ev)
        _CG.CFRelease(ev)
        return qt.class_at(cur.x, cur.y) == CANVAS_CLASS
    except Exception:
        return True


def _cursor_is_over_fusion():
    """
    Return True when the cursor is genuinely over Fusion's 3D viewport canvas.

    Two independent checks, because neither covers the other's blind spot:
      • Qt (_cursor_over_canvas_widget) — the authority on Fusion's own UI: it
        sees the widget under the cursor, so it rejects the chrome AX cannot
        see (S-menu dropdown, context menus, toolbars, timeline) — but it knows
        nothing about other applications' windows.
      • AX (below) — rejects foreign windows overlapping the viewport, and
        Fusion's separate palettes/dialogs, by requiring Fusion's MAIN window
        (AXRole 'AXWindow' + AXMain==true) under the cursor.

    Uses AXUIElementCopyElementAtPosition — the same API BTT uses for its
    Hovered UI Element Inspector.  Two filters:
      • PID == Fusion — catches dock context menus and other foreign windows
        that overlap the viewport bounds (without window-server z-order queries).
      • AXMain window — catches Fusion's OWN chrome (toolbars → AXButton,
        menus/dropdowns → AXMenuItem) AND its dialogs/panels (Preferences, etc.,
        which are AXMain=false even when large), so the orbit centre never fires
        through a menu, panel, or dialog opened over the viewport.
    Identifies the canvas by AXMain rather than subrole/size: the canvas is the
    main window whether it's the titled window OR the borderless one the Maximize
    Viewport add-in produces (subrole AXDialog). An earlier "AXDialog && large"
    test wrongly took big dialogs for the canvas. (Roles discovered empirically;
    see ax_hover_probe.)

    Returns True on any error so a permissions/API failure never blocks
    normal orbit-center use.
    """
    global _ax_system_element
    # Fusion's own chrome first — the case AX is blind to (see the block above).
    if not _cursor_over_canvas_widget():
        return False
    try:
        if not _AX or not _CG:
            return True
        # Create and cache the system-wide accessibility element
        if _ax_system_element is None:
            _ax_system_element = _AX.AXUIElementCreateSystemWide()
        if not _ax_system_element:
            return True
        # Current cursor position (Quartz / AX use the same coordinate space)
        ev  = _CG.CGEventCreate(None)
        cur = _CG.CGEventGetLocation(ev)
        _CG.CFRelease(ev)
        # Query the element at the cursor
        elem = ctypes.c_void_p(0)
        err  = _AX.AXUIElementCopyElementAtPosition(
            _ax_system_element,
            ctypes.c_float(cur.x),
            ctypes.c_float(cur.y),
            ctypes.byref(elem),
        )
        if err != 0 or not elem.value:
            return True   # kAXErrorAPIDisabled or no element — fail open
        # Get the owning process PID
        pid = ctypes.c_int32(0)
        _AX.AXUIElementGetPid(elem.value, ctypes.byref(pid))
        if pid.value != _fusion_pid:
            _CG.CFRelease(elem.value)
            return False
        # Belongs to Fusion — now require it to be the canvas (standard window),
        # not a menu/dropdown/dialog Fusion drew over the viewport.
        role = _ax_str_attr(elem.value, 'AXRole')
        if role != 'AXWindow':
            _CG.CFRelease(elem.value)
            # role unreadable → fail open (True); a real non-window (button/menu/
            # panel) → not the canvas (False).
            return True if role is None else False
        # Canvas == the app's MAIN window (true for both the titled window and the
        # borderless maximized canvas). Fusion's own dialogs/panels are windows too
        # but are AXMain=false EVEN WHEN LARGE — so gate on AXMain, not subrole/size
        # (the old "AXDialog && size>600x400" test took big dialogs for the canvas
        # and fired the orbit centre / zoom over them). Matches reanchor + filter.
        is_main = _ax_bool_attr(elem.value, 'AXMain')
        if not is_main:
            # AXMain unreadable OR false → fall back to "fills the display" (never a
            # dialog). Fusion can report its only (canvas) window as AXMain=false
            # (2026-10-05 in reanchor, 2026-10-10 here: one 3440x1440 AXDialog,
            # main=0) — trusting that rejected every orbit-centre request.
            sz  = _ax_window_size(elem.value)
            scr = (_CG.CGDisplayPixelsWide(_CG.CGMainDisplayID()),
                   _CG.CGDisplayPixelsHigh(_CG.CGMainDisplayID()))
            ok  = bool(sz and sz[0] >= 0.9 * scr[0] and sz[1] >= 0.9 * scr[1])
        else:
            ok = is_main
        _CG.CFRelease(elem.value)
        return ok
    except Exception:
        return True


def _has_text_focus():
    """
    Returns True when it's unsafe to synthesize keystrokes/clicks:
      - Fusion is not the frontmost application (e.g. dock menu is open)
      - a menu-bar menu is pulled down
      - a modal dialog or sheet is open
      - a text field / combo box has keyboard focus
    """
    try:
        if not _OBJC:
            return False
        NSApp_cls = _OBJC.objc_getClass(b'NSApplication')
        ns_app    = _o_ptr(NSApp_cls, b'sharedApplication')

        # Fusion must be the active (frontmost) application.
        # If not (e.g. dock context menu, another app), keystrokes sent via
        # kCGHIDEventTap go to whatever is frontmost — not Fusion.
        f = _OBJC.objc_msgSend
        f.restype  = ctypes.c_bool
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if not f(ns_app, _OBJC.sel_registerName(b'isActive')):
            return True

        # A menu-bar menu is pulled down (File, Edit, the Apple menu, ...).
        # This is the ONE piece of Fusion chrome neither existing gate can see, and
        # it is why sliding down an open menu over the viewport used to shut it:
        #   • Qt (_cursor_over_canvas_widget) sees no popup and reports QTCanvas
        #     under the cursor — a native NSMenu is not a Qt widget, so it may as
        #     well not exist;
        #   • AX (_cursor_is_over_fusion) asks what is at the point and Fusion, being
        #     AX-blind, answers with its main window — AXMain=true, so it passes;
        #   • isActive above is true, because Fusion stays the active application
        #     while its OWN menu tracks.
        # So the 1-finger dwell looked like a dwell over bare canvas and fired the
        # orbit centre, whose ⌘I/⌘U went to the tracking menu and dismissed it.
        # NSMenu.highlightedItem on the main menu is the exact signal: the top-level
        # item stays highlighted for exactly as long as its menu is pulled down, and
        # is nil the rest of the time — so this costs nothing when no menu is open
        # and cannot misfire on tooltips or Qt popups (a window-layer test would).
        main_menu = _o_ptr(ns_app, b'mainMenu')
        if main_menu and _o_ptr(main_menu, b'highlightedItem'):
            return True

        # Modal window open (file dialogs, alerts, etc.)
        if _o_ptr(ns_app, b'modalWindow'):
            return True

        key_win = _o_ptr(ns_app, b'keyWindow')
        if not key_win:
            return True   # nothing focused — be safe

        # Sheet attached to the key window
        if _o_ptr(key_win, b'attachedSheet'):
            return True

        # Text input has focus
        resp = _o_ptr(key_win, b'firstResponder')
        if resp:
            for cls_name in (b'NSTextField', b'NSTextView',
                             b'NSComboBox', b'NSSearchField'):
                cls = _OBJC.objc_getClass(cls_name)
                if not cls:
                    continue
                f = _OBJC.objc_msgSend
                f.restype  = ctypes.c_bool
                f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                if f(resp, _OBJC.sel_registerName(b'isKindOfClass:'), cls):
                    return True
        return False
    except Exception:
        return False


def _is_face_on_ortho(vp):
    """True when camera is orthographic AND aimed along a principal axis (named views only)."""
    try:
        cam = vp.camera
        if cam.cameraType != adsk.core.CameraTypes.OrthographicCameraType:
            return False
        eye = cam.eye
        tgt = cam.target
        dx = tgt.x - eye.x
        dy = tgt.y - eye.y
        dz = tgt.z - eye.z
        length = math.sqrt(dx*dx + dy*dy + dz*dz)
        if length < 1e-9:
            return False
        dx /= length; dy /= length; dz /= length
        # Named views (Front/Back/Left/Right/Top/Bottom) snap exactly to an axis;
        # any real orbit deviates by more than this threshold immediately.
        THRESH = 0.001
        return (1.0 - abs(dx) < THRESH or
                1.0 - abs(dy) < THRESH or
                1.0 - abs(dz) < THRESH)
    except Exception:
        return False


def _in_sketch():
    """True when actively editing a sketch (a drawing tool is available to receive clicks)."""
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        return design is not None and fvs.is_editing_sketch(design)
    except Exception:
        return False


def _orbit_pos_valid(req_pos):
    """
    True if the cursor is still within 2 px of where dwell fired.
    Rejects stale ORBIT_REQUESTs written while cursor was elsewhere
    (e.g. outside the viewport) that are processed only after the cursor
    has moved into the viewport.
    """
    if req_pos is None or not _CG:
        return True   # no position stored — give benefit of the doubt
    try:
        ev  = _CG.CGEventCreate(None)
        cur = _CG.CGEventGetLocation(ev)
        _CG.CFRelease(ev)
        return abs(int(cur.x) - req_pos[0]) <= 2 and abs(int(cur.y) - req_pos[1]) <= 2
    except Exception:
        return True


# ── Click / selection-drag guard ──────────────────────────────────────────────
# The orbit-center action fires a synthetic click, which is destructive in two
# situations:
#   • Shift/Command held — an extend/modify-selection click: the orbit click
#     clears the selection and starts a new one instead of adding to it.
#   • A left-drag in progress (e.g. dragging a rubber-band selection window,
#     common in sketch mode): the orbit click cancels the selection window.
# So we suppress while either is true. The catch: macOS "three finger drag"
# synthesises a LEFT-button drag, so a three-finger orbit also reports the left
# button down — and we must NOT suppress those (the orbit should run normally).
# reanchor drives a three-finger orbit with the MIDDLE button (it posts an MMB
# down and holds it for the whole drag), so a left-down WITH the middle button
# down is reanchor's orbit, not a selection. That distinguishes the two without
# needing a finger count. Leaving the request unhandled lets it fire later once
# the button/modifier is released.

def _orbit_suppressed():
    """Return True when the orbit centre should NOT fire (see header)."""
    try:
        if not _CG:
            return False
        flags = _CG.CGEventSourceFlagsState(_kCGEventSourceStateHIDSystemState)
        if flags & (_kCGEventFlagMaskShift | _kCGEventFlagMaskCommand):
            return True
        left_down = _CG.CGEventSourceButtonState(_kCGEventSourceStateHIDSystemState,
                                                 _kCGMouseButtonLeft)
        middle_down = _CG.CGEventSourceButtonState(_kCGEventSourceStateHIDSystemState,
                                                   _kCGMouseButtonCenter)
        # Left down + middle up  → a real left-drag (selection window) → suppress.
        # Left down + middle down → reanchor's three-finger orbit → let it run.
        if left_down and not middle_down:
            return True
        return False
    except Exception:
        return False


# ── Pivot distance (for FusionScrollFilter) ───────────────────────────────────

def _do_fling(vp, direction):
    """Teleport the camera to a cleanly-framed boundary on a fast-swipe fling.

      'out' → target = world origin, eye 10 m back along the current view dir
              (origin centred, orientation unchanged).
      'in'  → target = last orbit centre, eye 10 cm back along the view dir.
              Skipped when no real (geometry) orbit centre has been set.
    Instant snap (no smooth transition). View direction and up vector preserved.
    """
    try:
        cam = vp.camera
        eye = cam.eye; tgt = cam.target
        fx = tgt.x - eye.x; fy = tgt.y - eye.y; fz = tgt.z - eye.z
        fl = math.sqrt(fx*fx + fy*fy + fz*fz)
        if fl < 1e-9:
            return
        fx /= fl; fy /= fl; fz /= fl            # forward (eye→target) unit vector

        if direction == 'out':
            # Land the orbit centre UNDER THE CURSOR (not screen centre), 5 m back:
            # place it along the cursor's view ray at `dist`. Fall back to centred
            # if there's no orbit centre or no known cursor position.
            P    = _last_orbit_center if _last_orbit_center is not None else (0.0, 0.0, 0.0)
            dist = FLING_OUT_CM
            new_eye = new_tgt = None
            if _last_cur[0] is not None:
                try:
                    _, d = _ray_from_viewport(cam, _last_cur[0], _last_cur[1], vp.width, vp.height)
                    new_eye = (P[0] - d.x*dist, P[1] - d.y*dist, P[2] - d.z*dist)
                    new_tgt = (new_eye[0] + fx*dist, new_eye[1] + fy*dist, new_eye[2] + fz*dist)
                except Exception:
                    new_eye = new_tgt = None
            if new_eye is None:                 # centred fallback
                new_eye = (P[0] - fx*dist, P[1] - fy*dist, P[2] - fz*dist)
                new_tgt = P
        else:                                   # 'in'
            if _last_orbit_center is None:
                return                          # no real orbit centre → don't fling in
            P = _last_orbit_center
            dist = FLING_IN_CM
            new_eye = (P[0] - fx*dist, P[1] - fy*dist, P[2] - fz*dist)
            new_tgt = P

        cam.eye    = adsk.core.Point3D.create(*new_eye)
        cam.target = adsk.core.Point3D.create(*new_tgt)
        cam.isSmoothTransition = False
        vp.camera = cam
        vp.refresh()          # force an immediate repaint so the jump never lags
    except Exception:
        pass


def _write_pivot_distance(vp):
    """Write camera distances (cm) for FusionScrollFilter.

    PIVOT_DIST  = eye→pivot, where pivot is the nearest geometry under the cursor
                  (_last_hit) if known, else the camera target.
    ORIGIN_DIST = eye→model origin (0,0,0).

    Recomputed every fire — including mid-gesture while the probe is otherwise
    paused — so both values track the eye as it dollies. Cheap: reads the camera
    and does a couple of subtractions; no ray cast.

    Over empty space (no _last_hit) the pivot is _empty_ref() instead of the camera
    target, and ZOOM_GAIN asks the filter to scale the zoom so it moves as it would
    with that point under the cursor (§ Empty-space gain).
    """
    global _ref_hit
    try:
        cam = vp.camera
        eye = cam.eye
        gain = 1.0
        if _last_hit is not None:
            hx, hy, hz = _ref_hit = _last_hit
        else:
            t = cam.target
            hx, hy, hz = t.x, t.y, t.z
            r = _empty_ref(cam)
            if r is not None:
                (hx, hy, hz), depth, tdepth = r
                gain = min(ZOOM_GAIN_MAX, max(ZOOM_GAIN_MIN, depth / tdepth))
        dx = hx - eye.x; dy = hy - eye.y; dz = hz - eye.z
        d  = math.sqrt(dx*dx + dy*dy + dz*dz)
        od = math.sqrt(eye.x*eye.x + eye.y*eye.y + eye.z*eye.z)
        with open(PIVOT_DIST, 'w') as f:
            f.write('%.4f' % d)
        with open(ORIGIN_DIST, 'w') as f:
            f.write('%.4f' % od)
        with open(ZOOM_GAIN, 'w') as f:
            f.write('%.4f' % gain)
    except Exception:
        pass


# ── Empty-space gain ──────────────────────────────────────────────────────────
# Fusion zooms toward the point under the pointer; over empty space it uses the
# camera target's depth instead, which shrinks with every step — the zoom grinds to
# a halt (user, 2026-10-19: "zooming does virtually nothing"). The zoom still goes
# toward the pointer, but at the speed it would have with a real point under it:
# the last geometry the pointer was over (= the last sensitivity), else the model's
# bounding-box centre — whichever is in front of the eye. gain = its depth ÷ the
# target's depth, clamped; FusionScrollFilter multiplies the pinch's pixels by it.
# (Replaces the 2026-10-18 zoom anchor, which jumped the pointer to the model.)

_ref_hit     = None          # the last _last_hit seen (kept over empty space)
_bbox_centre = (0.0, None)   # (time, (x, y, z)) — the root bounding box is cached 10 s

def _model_centre():
    global _bbox_centre
    t, c = _bbox_centre
    if time.time() - t > 10.0:
        c = None
        design = adsk.fusion.Design.cast(_app.activeProduct)
        bb = design and design.rootComponent.boundingBox
        if bb:
            a, b = bb.minPoint, bb.maxPoint
            c = ((a.x+b.x)/2, (a.y+b.y)/2, (a.z+b.z)/2)
        _bbox_centre = (time.time(), c)
    return c


def _empty_ref(cam):
    """(point, its depth, the target's depth) along the view, or None."""
    eye, tgt = cam.eye, cam.target
    fx = tgt.x - eye.x; fy = tgt.y - eye.y; fz = tgt.z - eye.z
    tdepth = math.sqrt(fx*fx + fy*fy + fz*fz)
    if tdepth < 1e-6:
        return None
    for p in (_ref_hit, _model_centre()):
        if p is None:
            continue
        depth = ((p[0]-eye.x)*fx + (p[1]-eye.y)*fy + (p[2]-eye.z)*fz) / tdepth
        if depth > 0.5:
            return p, depth, tdepth
    return None


# ── Orbit-center synthesis ────────────────────────────────────────────────────

def _synthesize_orbit_center(geometry_under_cursor):
    """
    Execute the Set Orbit Center action directly, bypassing BTT.
    geometry_under_cursor=True  → "i" keydown/up + left click at cursor
    geometry_under_cursor=False → "u" keydown/up only
    """
    # Remember the orbit centre for in-flings: the geometry point under the
    # cursor when set on geometry, else None (free-space sets don't give us a
    # point, so an in-fling won't fire until a real one is set).
    global _last_orbit_center, _last_orbit_screen, _orbit_gen
    if geometry_under_cursor and _last_hit is not None:
        # Bump the generation if this orbit centre is on NEW geometry (moved more
        # than ORBIT_GEN_EPS from the last). A same-spot re-set (e.g. finger-lift
        # without moving) gives the identical hit point → no bump → lock holds.
        if (_last_orbit_center is None
                or ((_last_hit[0]-_last_orbit_center[0])**2
                    + (_last_hit[1]-_last_orbit_center[1])**2
                    + (_last_hit[2]-_last_orbit_center[2])**2) > ORBIT_GEN_EPS*ORBIT_GEN_EPS):
            _orbit_gen += 1
            try:
                with open(ORBIT_GEN_FILE, 'w') as f:
                    f.write(str(_orbit_gen))
            except Exception:
                pass
        _last_orbit_center = _last_hit
        _last_orbit_screen = _last_cur if _last_cur[0] is not None else None
    else:
        # Set over nothing. If we're still within ORBIT_KEEP_PX of where the last
        # geometry orbit centre was set, KEEP it — a slight slip off the edge of
        # geometry should still let an in-fling jump to that orbit centre.
        keep = False
        if _last_orbit_screen is not None and _last_cur[0] is not None:
            ddx = _last_cur[0] - _last_orbit_screen[0]
            ddy = _last_cur[1] - _last_orbit_screen[1]
            keep = (ddx*ddx + ddy*ddy) <= (ORBIT_KEEP_PX * ORBIT_KEEP_PX)
        if not keep:
            _last_orbit_center = None
            _last_orbit_screen = None
    try:
        if not _CG:
            return
        src = _CG.CGEventSourceCreate(_kCGEventSourceStateHIDSystemState)
        _CG.CGEventSourceSetUserData(src, _CURSOR_PROBE_MARKER)

        # Read current cursor position for the click (geometry case)
        ev  = _CG.CGEventCreate(None)
        pos = _CG.CGEventGetLocation(ev)
        _CG.CFRelease(ev)

        vk = _VK_ANSI_I if geometry_under_cursor else _VK_ANSI_U
        if geometry_under_cursor and _slice_hit:
            _slice_pickable(True)

        key_dn = _CG.CGEventCreateKeyboardEvent(src, vk, True)
        _CG.CGEventSetFlags(key_dn, _kCGEventFlagMaskCommand)
        _CG.CGEventPost(_kCGHIDEventTap, key_dn)
        _CG.CFRelease(key_dn)

        key_up = _CG.CGEventCreateKeyboardEvent(src, vk, False)
        _CG.CGEventSetFlags(key_up, _kCGEventFlagMaskCommand)
        _CG.CGEventPost(_kCGHIDEventTap, key_up)
        _CG.CFRelease(key_up)

        if geometry_under_cursor:
            m_dn = _CG.CGEventCreateMouseEvent(
                src, _kCGEventLeftMouseDown, pos, _kCGMouseButtonLeft)
            _CG.CGEventSetFlags(m_dn, 0)
            _CG.CGEventPost(_kCGHIDEventTap, m_dn)
            _CG.CFRelease(m_dn)

            m_up = _CG.CGEventCreateMouseEvent(
                src, _kCGEventLeftMouseUp, pos, _kCGMouseButtonLeft)
            _CG.CGEventSetFlags(m_up, 0)
            _CG.CGEventPost(_kCGHIDEventTap, m_up)
            _CG.CFRelease(m_up)

        _CG.CFRelease(src)
    except Exception:
        pass


# ── Custom-event handler (Fusion main thread) ─────────────────────────────────

class _PollHandler(adsk.core.CustomEventHandler):
    def __init__(self): super().__init__()

    def notify(self, args):
        global _pending, _state, _last_cur
        _pending = False
        if _slice_unpick_at and time.time() > _slice_unpick_at:
            _slice_pickable(False)
        _note_ui_context()
        suppressed    = _orbit_suppressed()
        has_request   = os.path.exists(REQUEST)
        has_orbit     = False
        _orbit_req_pos = None
        if os.path.exists(ORBIT_REQUEST):
            has_orbit = True
            try:
                with open(ORBIT_REQUEST) as _f:
                    _px, _py = _f.read().strip().split(',')
                    _orbit_req_pos = (int(_px), int(_py))
            except Exception:
                pass

        try:
            if not _app or not _app.isValid:
                return
            vp = _app.activeViewport
            if not vp:
                return
            if not adsk.fusion.Design.cast(_app.activeProduct):
                return

            # Fling: the filter dropped an "in"/"out" request here on a fast-swipe
            # flick. Execute the camera jump and clear the file, before anything else.
            if os.path.exists(FLING_FILE):
                _dir = ''
                try:
                    with open(FLING_FILE) as _f:
                        _dir = _f.read().strip()
                finally:
                    try:
                        os.remove(FLING_FILE)
                    except Exception:
                        pass
                if _dir in ('in', 'out'):
                    _do_fling(vp, _dir)
                return

            if _is_face_on_ortho(vp):
                _last_cur = (None, None)   # force fresh geometry check on re-entry
                return

            if _in_sketch() and vp.camera.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
                _last_cur = (None, None)
                return

            # Pivot-distance for FusionScrollFilter. During a 2-finger gesture
            # (PAUSE present) the cursor is stationary and ray casting is paused,
            # but the eye keeps dollying in — so just recompute eye→_last_hit and
            # bail. This keeps the zoom-taper live without doing any ray work.
            if os.path.exists(PAUSE) and not has_request and not has_orbit:
                _write_pivot_distance(vp)
                return
            # Otherwise keep the file fresh every fire too (even when the cursor
            # is stationary and we skip the ray cast below).
            _write_pivot_distance(vp)

            vp_x, vp_y = _mouse_viewport_pos(vp)
            cursor_stationary = (vp_x is not None) and ((vp_x, vp_y) == _last_cur)

            if vp_x is None:
                found = False
            else:
                # Cursor-stationary skip — but bypass it on an on-demand REQUEST
                # so the result is guaranteed fresh at finger-lift.
                if cursor_stationary and _state is not None and not has_request and not has_orbit:
                    return
                _last_cur = (vp_x, vp_y)
                result = _find_geometry(_app, vp, vp_x, vp_y)
                if result is None:
                    # Ray cast failed — fall through so orbit still fires with cached state
                    if has_orbit and not suppressed \
                            and not os.path.exists(PAUSE) \
                            and not os.path.exists(ORBIT_PAUSE) \
                            and not _has_text_focus() \
                            and _cursor_is_over_fusion() \
                            and not _ui_blocked_recently() \
                            and _orbit_pos_valid(_orbit_req_pos):
                        _synthesize_orbit_center(_state if _state is not None else False)
                    return
                found = result

            if found != _state:
                _state = found
                try:
                    with open(OUTPUT, 'w') as f:
                        f.write('1' if found else '0')
                except Exception:
                    pass
                try:
                    trigger = 'fusion_geometry_on' if found else 'fusion_geometry_off'
                    _url = f'http://127.0.0.1:12321/trigger_named/?trigger_name={trigger}'
                    threading.Thread(
                        target=lambda u=_url: urllib.request.urlopen(u, timeout=1),
                        daemon=True,
                    ).start()
                except Exception:
                    pass

            # Execute orbit center action — cursor must still be where dwell fired
            # and the AX element at the cursor must belong to Fusion (not Dock etc.)
            if has_orbit and not suppressed \
                    and vp_x is not None \
                    and not os.path.exists(PAUSE) \
                    and not os.path.exists(ORBIT_PAUSE) \
                    and not _has_text_focus() \
                    and _cursor_is_over_fusion() \
                    and not _ui_blocked_recently() \
                    and _orbit_pos_valid(_orbit_req_pos):
                _synthesize_orbit_center(_state if _state is not None else False)

        except Exception:
            pass
        finally:
            # Always unblock BTT's shell script — even on early returns or errors.
            if has_request:
                try:
                    os.remove(REQUEST)
                except Exception:
                    pass
            if has_orbit and not suppressed:
                try:
                    os.remove(ORBIT_REQUEST)
                except Exception:
                    pass


# ── Background poll thread ────────────────────────────────────────────────────

def _poll_loop():
    """
    Inner cadence is 5 ms so an on-demand REQUEST file is detected fast.
    Background polling still runs at INTERVAL (50 ms / 20 Hz) and is gated
    by PAUSE.  REQUEST bypasses both: it always fires immediately.
    """
    global _pending
    last_fire = 0.0
    while _running:
        now       = time.time()
        on_demand = os.path.exists(REQUEST)
        orbit_req = os.path.exists(ORBIT_REQUEST)
        fling_req = os.path.exists(FLING_FILE)        # fire immediately to land the fling
        paused    = os.path.exists(PAUSE)
        due       = (now - last_fire) >= INTERVAL and not paused
        # While paused (mid-gesture) keep firing at DIST_INTERVAL purely to push a
        # live pivot distance to FusionScrollFilter; the handler short-circuits to
        # the cheap distance-only path in that case.
        dist_due  = paused and (now - last_fire) >= DIST_INTERVAL

        if (on_demand or orbit_req or fling_req or due or dist_due) and not _pending:
            _pending = True
            try:
                _app.fireCustomEvent(EVENT_ID)
                last_fire = now
            except Exception:
                _pending = False

        time.sleep(0.005)   # 5 ms — cheap stat() of REQUEST/PAUSE


# ── Add-in lifecycle ──────────────────────────────────────────────────────────

def run(context):
    global _app, _ui, _running, _thread, _handler, _cmd_end_handler
    try:
        _app = adsk.core.Application.get()
        _ui  = _app.userInterface

        event    = _app.registerCustomEvent(EVENT_ID)
        _handler = _PollHandler()
        event.add(_handler)
        _cmd_end_handler = _OnCommandEnd()
        _ui.commandTerminated.add(_cmd_end_handler)

        _running = True
        _thread  = threading.Thread(target=_poll_loop, daemon=True)
        _thread.start()

        adsk.autoTerminate(False)

    except Exception:
        if _ui:
            _ui.messageBox('CursorProbe ERROR:\n' + traceback.format_exc())


def stop(context):
    global _running
    _running = False
    if _slice_unpick_at:
        _slice_pickable(False)
    try:
        _ui.commandTerminated.remove(_cmd_end_handler)
    except Exception:
        pass
    try:
        _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
