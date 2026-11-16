"""UI Zoom Hold — the model keeps its size on the glass through UI Zoom (2026-10-29).

UI Zoom (Scripts/ui_zoom.sh § Screen zoom) magnifies the whole screen and re-fits
every tiled window to the visible part: Fusion's window shrinks by a/b in points
(a = the old zoom, b = the new) and the window server magnifies it by b — so the
toolbars and our windows grow, and so did the model. The user wants the interfaces
to grow and the MODEL to stay put (like Photoshop's canvas, pscursor § UI Zoom).

So: whenever the viewport is re-sized for a zoom change (both sides by a/b, the
zoom in /tmp/.screen_zoom_to differs from the one the old size was taken at), the
camera is set so the model's scale in view points is s0 × a/b — the same on the
glass. Measured, not assumed: s = view points per 10 cm along the camera's up
vector at its target, before (kept fresh by camera events / a slow tick) and after,
and the camera moved by the difference (perspective: the eye along its line to the
target; orthographic: viewExtents), re-measured up to 3 times. A resize while
Fusion was off screen (slots re-fit it on reveal) is caught the same way: the zoom
the old size was taken at is remembered. Any other resize (a slot layout change)
just becomes the new baseline.

Log: /tmp/fusion_ui_zoom.log ("fusion by itself" = what Fusion did to the scale on
the resize, before our correction).
The glide's stand-in freezes the canvas meanwhile (Scripts/ui_zoom/stand_in.json).
"""
import adsk.core
import os
import threading
import time
import traceback

EVENT_ID = 'JoshUIZoomHoldTick'
ZOOM_TO = '/tmp/.screen_zoom_to'       # ui_zoom.sh writes it before each glide
ZOOM_NOW = '/tmp/.screen_zoom'         # ...and this when it lands
LOG = '/tmp/fusion_ui_zoom.log'
SLOW_S = 1.0                           # idle tick (catches slots' later re-fit)
FAST_S = 0.05                          # after a zoom press, for FAST_FOR_S
FAST_FOR_S = 4.0
GLIDE_S = 2.0                          # a zoom press this recent: a resize may be mid-glide

_app = None
_ui = None
_handlers = []
_running = False
_pending = False
_rec = None                            # [w, h, scale, zoom] at the last settled size
_setting = False


def _log(msg):
    try:
        with open(LOG, 'a') as f:
            f.write(time.strftime('%m-%d %H:%M:%S ') + msg + '\n')
    except Exception:
        pass


def _read_zoom(path):
    try:
        with open(path) as f:
            z = float(f.read().strip())
        return z if z >= 1 else 1.0
    except Exception:
        return None


def _zoom_target():
    z = _read_zoom(ZOOM_TO)
    return z if z is not None else (_read_zoom(ZOOM_NOW) or 1.0)


def _since_press():
    try:
        return time.time() - os.stat(ZOOM_TO).st_mtime
    except Exception:
        return 1e9


def _scale(vp):
    """View points per 10 cm along the camera's up vector, at its target."""
    cam = vp.camera
    t, up = cam.target, cam.upVector
    p = adsk.core.Point3D.create(t.x + up.x * 10, t.y + up.y * 10, t.z + up.z * 10)
    a, b = vp.modelToViewSpace(t), vp.modelToViewSpace(p)
    if not a or not b:
        return 0.0
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _correct(vp, want):
    global _setting
    _setting = True
    try:
        for _ in range(3):
            s = _scale(vp)
            if s <= 0 or abs(s / want - 1) < 0.003:
                break
            f = s / want                                   # > 1: too big, move away
            cam = vp.camera
            cam.isSmoothTransition = False
            if cam.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
                cam.viewExtents = cam.viewExtents * f
            else:
                t, e = cam.target, cam.eye
                cam.eye = adsk.core.Point3D.create(t.x + (e.x - t.x) * f, t.y + (e.y - t.y) * f,
                                                   t.z + (e.z - t.z) * f)
            vp.camera = cam
    finally:
        _setting = False
    return _scale(vp)


_told_press = 0.0                      # the press last reported on


def _report_press(w, h):
    """Once per press, GLIDE_S after it: was the window re-fitted at all? Not re-fitted
    (left at the old zoom's size, e.g. off screen) = Fusion simply magnified, model too."""
    global _told_press
    try:
        t = os.stat(ZOOM_TO).st_mtime
    except Exception:
        return
    if t == _told_press or time.time() - t < GLIDE_S:
        return
    _told_press = t
    z = _zoom_target()
    _log('press -> zoom %.2f: viewport %dx%d (a full tile at this zoom: %dx%d)%s'
         % (z, w, h, round(3440 / z), round(1440 / z),
            '' if abs(w * z - 3440) < 40 or abs(w * z - 1720) < 40 else '  NOT RE-FITTED?'))


def _check(camera_event=False):
    global _rec
    vp = _app.activeViewport
    if not vp:
        return
    w, h = vp.width, vp.height
    if w <= 0 or h <= 0:
        return
    if not camera_event:
        _report_press(w, h)
    if _rec is None:
        _rec = [w, h, _scale(vp), _read_zoom(ZOOM_NOW) or 1.0]
        return
    rw, rh, rs, rz = _rec
    if (w, h) == (rw, rh):
        _rec[2] = _scale(vp)                               # the camera moved: keep s0 fresh
        return
    if camera_event:
        return                                             # resizes are judged on the tick
    z = _zoom_target()
    r = w / rw
    if abs(z - rz) > 0.004 and abs(r - rz / z) < 0.03 and abs(h / rh - r) < 0.05:
        want = rs * rz / z
        got = _scale(vp)
        after = _correct(vp, want)
        _log('zoom %.2f -> %.2f: viewport %dx%d -> %dx%d, scale %.1f -> %.1f (fusion by itself), '
             'want %.1f, now %.1f' % (rz, z, rw, rh, w, h, rs, got, want, after))
        _rec = [w, h, after, z]
    elif _since_press() < GLIDE_S:
        return                                             # a step of a running glide: wait for its end
    else:
        _log('resize %dx%d -> %dx%d at zoom %.2f: new baseline' % (rw, rh, w, h, z))
        _rec = [w, h, _scale(vp), z]


class _TickHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _pending
        try:
            _check()
        except Exception:
            _log('tick failed: ' + traceback.format_exc().replace('\n', ' | '))
        finally:
            _pending = False


class _CameraHandler(adsk.core.CameraEventHandler):
    def notify(self, args):
        if _setting:
            return
        try:
            _check(camera_event=True)
        except Exception:
            pass


def _poll_loop():
    global _pending
    last = 0.0
    while _running:
        now = time.time()
        every = FAST_S if _since_press() < FAST_FOR_S else SLOW_S
        if now - last >= every and not _pending:
            _pending = True
            try:
                _app.fireCustomEvent(EVENT_ID)
                last = now
            except Exception:
                _pending = False
        time.sleep(0.025)


def run(context):
    global _app, _ui, _running
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        ev = _app.registerCustomEvent(EVENT_ID)
        h = _TickHandler()
        ev.add(h)
        _handlers.append(h)
        c = _CameraHandler()
        _app.cameraChanged.add(c)
        _handlers.append(c)
        _running = True
        threading.Thread(target=_poll_loop, daemon=True).start()
        adsk.autoTerminate(False)
    except Exception:
        if _ui:
            _ui.messageBox('UI Zoom Hold failed to start:\n' + traceback.format_exc())


def stop(context):
    global _running
    _running = False
    try:
        _app.cameraChanged.remove(_handlers[1])
    except Exception:
        pass
    try:
        _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _handlers.clear()
