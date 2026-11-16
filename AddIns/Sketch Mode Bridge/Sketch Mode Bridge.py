import adsk.core
import adsk.fusion
import os
import threading
import traceback
import types as _types
import urllib.request

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


def get_sketch_state() -> str:
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return 'normal'
        # fvs, not design.activeEditObject: the raw property raises for a sketch
        # nested in an occurrence, so the bare except below reported 'normal' and
        # BTT never saw sketch mode for anything below the top level.
        return 'sketch' if fvs.is_editing_sketch(design) else 'normal'
    except Exception:
        return 'normal'


def push_state(state: str):
    global _last_state
    if state == _last_state:
        return
    _last_state = state
    if state == 'sketch':
        trigger_btt('fusion_sketch_enter')
    else:
        trigger_btt('fusion_sketch_exit')


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            push_state(get_sketch_state())
        except Exception:
            pass


def run(context):
    global app, ui, handlers
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        push_state(get_sketch_state())

        handler = CommandTerminatedHandler()
        ui.commandTerminated.add(handler)
        handlers.append(handler)

    except Exception:
        if ui:
            ui.messageBox('SketchModeBridge failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global handlers
    try:
        handlers.clear()
        trigger_btt('fusion_sketch_exit')
    except Exception:
        pass
