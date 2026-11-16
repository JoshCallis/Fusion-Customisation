import adsk.core
import adsk.fusion
import threading
import traceback
import urllib.request

app = None
ui = None
handlers = []

_last_state = None


_BTT_URL = 'http://127.0.0.1:12321/trigger_named/?trigger_name={}'

def trigger_btt(trigger_name: str):
    def _fire():
        try:
            urllib.request.urlopen(_BTT_URL.format(trigger_name), timeout=1)
        except Exception:
            pass
    threading.Thread(target=_fire, daemon=True).start()


def push_state(state: str):
    global _last_state
    if state == _last_state:
        return
    _last_state = state
    if state == 'ortho':
        trigger_btt('fusion_ortho_on')
    else:
        trigger_btt('fusion_ortho_off')


def get_view_state() -> str:
    try:
        viewport = app.activeViewport
        if viewport is None:
            return 'perspective'
        cam = viewport.camera
        if cam.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
            return 'ortho'
        else:
            return 'perspective'
    except Exception:
        return 'perspective'


class CameraChangedHandler(adsk.core.CameraEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            push_state(get_view_state())
        except Exception:
            pass


def run(context):
    global app, ui, handlers
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Push initial state to BTT on startup
        push_state(get_view_state())

        # Listen for camera changes
        cam_handler = CameraChangedHandler()
        app.cameraChanged.add(cam_handler)
        handlers.append(cam_handler)

    except Exception:
        if ui:
            ui.messageBox('ViewStateBridge failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global handlers
    try:
        handlers.clear()
        # Tell BTT we're back to perspective when addin stops
        trigger_btt('fusion_ortho_off')
    except Exception:
        pass
