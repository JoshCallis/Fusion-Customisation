"""
Snap View
─────────
One command, "Snap View to Nearest Axis": turns the camera to whichever of the six
world directions (±X, ±Y, ±Z) it is already closest to looking along — front,
back, left, right, top or bottom — and STAYS IN PERSPECTIVE. Fusion's ViewCube
faces do the same turn but switch to orthographic (and Look At leaves you there).

  - the target and the eye's distance from it are kept: it's an orbit about the
    point you're looking at, not a zoom or a re-frame;
  - up is snapped too, to the world axis nearest the current screen up that is
    square to the new direction — a side view comes out Z-up (world Z is up here,
    as in Sketch Look At), a top/bottom view keeps whichever edge was lowest on
    screen lowest;
  - an orthographic camera is switched to perspective.

ZOOMED RIGHT OUT (or looking away): when the model's box is only a few pixels on
screen (`FAR_FRACTION` of the viewport's shorter side) or not on screen at all, the
snap also fits the view (Fusion's Fit — `isFitView`), so you land on the model
instead of an orbit about empty space. Judged on the root component's bounding box
projected to the screen (`_lost`); an empty design never counts.

Bind it from Control Room ▸ Fusion like any other command (id SnapViewNearestCmd).

VETOES ITSELF
─────────────
Fusion runs one command at a time. Doing the snap from commandCreated made this the
ACTIVE command, which ended whatever was running — pressing the key mid-Line finished
the Line. So the command is refused in commandStarting and the camera is turned there
(the pattern Sketch Dimensions uses): whatever was running keeps running.
"""
import adsk.core
import adsk.fusion
import traceback

CMD_ID   = 'SnapViewNearestCmd'
CMD_NAME = 'Snap View to Nearest Axis'
CMD_DESC = ('Turn the camera to the nearest front/back/left/right/top/bottom '
            'direction, staying in perspective')

_AXES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

# The model is "lost" when its on-screen box is no bigger than this share of the
# viewport's shorter side (1% ≈ 14 px on a 1440-px-high viewport).
FAR_FRACTION = 0.01

_app = None
_ui = None
_handlers = []
_starting = None


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _nearest(v, candidates):
    return max(candidates, key=lambda c: _dot(v, c))


def _lost(vp, eye, look):
    """True when the model is a few pixels on screen or off it entirely. `look` is the
    unit view direction (a corner behind the eye can't be projected meaningfully)."""
    des = adsk.fusion.Design.cast(_app.activeProduct)
    if not des:
        return False
    bb = des.rootComponent.boundingBox
    if not bb:
        return False
    lo, hi = bb.minPoint, bb.maxPoint
    if lo.distanceTo(hi) < 1e-9 or lo.x > hi.x:
        return False                     # nothing in the design
    xs, ys = [], []
    for x in (lo.x, hi.x):
        for y in (lo.y, hi.y):
            for z in (lo.z, hi.z):
                p = adsk.core.Point3D.create(x, y, z)
                if eye.vectorTo(p).dotProduct(look) <= 0:
                    continue
                v = vp.modelToViewSpace(p)
                xs.append(v.x)
                ys.append(v.y)
    if not xs:
        return True                      # all of it behind the camera
    w, h = vp.width, vp.height
    if max(xs) < 0 or min(xs) > w or max(ys) < 0 or min(ys) > h:
        return True                      # off screen
    return max(max(xs) - min(xs), max(ys) - min(ys)) <= FAR_FRACTION * min(w, h)


def snap():
    vp = _app.activeViewport
    if not vp:
        return
    cam = vp.camera
    eye, target = cam.eye, cam.target
    look = eye.vectorTo(target)
    dist = look.length
    if dist < 1e-9:
        return
    look.normalize()
    up = cam.upVector
    up.normalize()
    try:
        fit = _lost(vp, eye, look)
    except Exception:
        fit = False
        _log('lost check failed: ' + traceback.format_exc().replace('\n', ' | '))

    d = _nearest(look.asArray(), _AXES)
    # Up: the nearest axis square to the new direction.
    u = _nearest(up.asArray(), [a for a in _AXES if _dot(a, d) == 0])

    cam.cameraType = adsk.core.CameraTypes.PerspectiveCameraType
    cam.eye = adsk.core.Point3D.create(target.x - d[0] * dist,
                                       target.y - d[1] * dist,
                                       target.z - d[2] * dist)
    cam.target = target
    cam.upVector = adsk.core.Vector3D.create(*u)
    cam.isFitView = fit
    cam.isSmoothTransition = True
    vp.camera = cam


class _Created(adsk.core.CommandCreatedEventHandler):
    """Only reached if the veto below did not fire (a toolbar press with the handler
    gone); do the work here then — better a snap that ends a command than nothing."""
    def notify(self, args):
        try:
            snap()
        except Exception:
            _log('failed: ' + traceback.format_exc().replace('\n', ' | '))


class _Starting(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if self not in _handlers or args.commandId != CMD_ID:
                return
            args.isCanceled = True
            snap()
        except Exception:
            _log('veto failed: ' + traceback.format_exc().replace('\n', ' | '))


def _log(msg):
    # Runs while a command may be active, when a message box is an outrage.
    try:
        with open('/tmp/snap_view.log', 'a') as f:
            f.write(msg + '\n')
    except Exception:
        pass


def _panel():
    ws = _ui.workspaces.itemById('FusionSolidEnvironment')
    return ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')


def run(context):
    global _app, _ui, _starting
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        old = _ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()
        cd = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        h = _Created()
        cd.commandCreated.add(h)
        _handlers.append(h)
        panel = _panel()
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
        panel.controls.addCommand(cd)
        _starting = _Starting()
        _ui.commandStarting.add(_starting)
        _handlers.append(_starting)
    except Exception:
        if _ui:
            _ui.messageBox('Snap View start failed:\n' + traceback.format_exc())


def stop(context):
    global _starting
    try:
        if _starting is not None:
            try:
                _ui.commandStarting.remove(_starting)
            except Exception:
                pass
            _starting = None
        try:
            ctrl = _panel().controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        except Exception:
            pass
        cd = _ui.commandDefinitions.itemById(CMD_ID)
        if cd:
            cd.deleteMe()
        _handlers.clear()
    except Exception:
        pass
