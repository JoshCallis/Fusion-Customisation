"""
Toggle Isolation
────────────────
Toggles isolation of the active component — isolated ↔ off — then re-applies the
folder policy (see fvs.toggle_isolate / fvs.apply_policy).

Transient model — works exactly like Toggle Body / Toggle Sketch Visibility:
  • Default is ISOLATED: when you jump into a component (via the Browser or the
    nav add-ins) you see only that component and its children. The parent +
    siblings view that used to be a step here is now automatic, per kind: see
    Cycle Visibility (a body/sketch toggle with nothing to show falls back a level).
  • Press: OFF — every sibling/ancestor that isolation hid comes back on, minus
    anything *you* hid yourself (restored from the snapshot).
  • In a sketch edit with bodies AND sketches both off, only the edited sketch
    shows and un-isolating would reveal nothing — so that press turns BODIES on
    instead (the step of the visibility cycle that gives isolation something to
    act on) and leaves isolation where it was.  The next press toggles as normal.
  • Press again re-isolates.  OFF reverts to isolated the moment the active
    component changes — apply_policy resets the transient flag and whoever
    changed the activation has already re-isolated the new component.

Checkbox semantics: checked == fully isolated (the default-on state).
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

CMD_ID   = 'ToggleIsolationCmd'
CMD_NAME = 'Toggle Isolation'
CMD_DESC = "Toggle isolation: active component only ↔ everything."
PANEL_ID = 'SolidScriptsAddinsPanel'

app = None
ui = None
handlers = []


class _OnExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            isolated = fvs.toggle_isolate(design)   # True == isolated
            cmd_def = ui.commandDefinitions.itemById(CMD_ID)
            if cmd_def:
                try:
                    cmd_def.isChecked = isolated
                except Exception:
                    pass
        except Exception:
            ui.messageBox('ToggleIsolation – Execute error:\n' + traceback.format_exc())


class _OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            on_exec = _OnExecute()
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
        except Exception:
            ui.messageBox('ToggleIsolation – CommandCreated error:\n' + traceback.format_exc())


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # Find-or-create — never delete-then-create — so keyboard shortcuts keyed
        # by CMD_ID survive a reload.
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            is_checked = not fss.get_value('isolate_off', False)
            try:
                cmd_def = ui.commandDefinitions.addCheckBoxDefinition(
                    CMD_ID, CMD_NAME, CMD_DESC, is_checked)
            except Exception:
                cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_created = _OnCommandCreated()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        if ui:
            ui.messageBox('ToggleIsolation – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        # Clear handlers (old handler objects fail their `self in handlers` guard)
        # and remove the toolbar control, but NEVER delete the command definition —
        # that would destroy the keyboard-shortcut binding.
        handlers.clear()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
    except Exception:
        if ui:
            ui.messageBox('ToggleIsolation – stop error:\n' + traceback.format_exc())
