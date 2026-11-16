"""
Toggle Sketch Visibility
────────────────────────
Temporarily flips the active component's own sketch visibility and re-applies the
folder policy (see fvs.apply_policy / fvs.toggle_sketches).

Transient, leaf-aware model:
  • Default by depth — a bottom-level (leaf) component shows its sketches by
    default; a non-leaf component hides them.
  • This toggle flips that for the CURRENT active component only; the state
    reverts to the depth-based default the moment the active component changes.
  • While editing a sketch it shows/hides the OTHER sketches in the active
    component, never the sketch being edited.
"""

import adsk.core, adsk.fusion, traceback, os
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fss = _load('fss', 'Shared State.py')
fvs = _load('fvs', 'Visibility Snapshot.py')

CMD_ID   = 'ToggleSketchVisibilityCmd'
CMD_NAME = 'Toggle Sketch Visibility'
CMD_DESC = 'Show only the active component\'s sketch folder, or hide all sketch folders.'
STATE_KEY = 'sketches_visible'

app = None
ui = None
handlers = []


class _OnExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            new_state = fvs.toggle_sketches(design)
            cmd_def = ui.commandDefinitions.itemById(CMD_ID)
            if cmd_def:
                try:
                    cmd_def.isChecked = new_state
                except Exception:
                    pass
        except Exception:
            ui.messageBox('ToggleSketchVisibility – Execute error:\n' + traceback.format_exc())


class _OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            on_exec = _OnExecute()
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
        except Exception:
            ui.messageBox('ToggleSketchVisibility – CommandCreated error:\n' + traceback.format_exc())


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        existing = ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()

        is_checked = bool(fss.get_value(STATE_KEY, True))
        try:
            cmd_def = ui.commandDefinitions.addCheckBoxDefinition(
                CMD_ID, CMD_NAME, CMD_DESC, is_checked)
        except Exception:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_created = _OnCommandCreated()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        if ui:
            ui.messageBox('ToggleSketchVisibility – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        handlers.clear()
    except Exception:
        if ui:
            ui.messageBox('ToggleSketchVisibility – stop error:\n' + traceback.format_exc())
