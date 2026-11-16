import adsk.core
import adsk.fusion
import traceback
import os
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fvs = _load('fvs', 'Visibility Snapshot.py')
skp = _load('skp', 'Sketch Picks.py')

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = 'HideAndDeselectCmd'
CMD_NAME = 'Hide && Deselect'
CMD_DESC = 'Hide selected objects and clear selection'

handlers = []


# ---------- EXECUTE ----------
class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            selection = ui.activeSelections
            if selection.count == 0:
                return

            to_hide = []

            # Collect entities first (avoid modifying selection mid-loop)
            for i in range(selection.count):
                ent = selection.item(i).entity
                to_hide.append(ent)
            # A Browser sketch pick is its curves + the Sketch: hide the sketch, and
            # never let a curve proxy fall through to hiding its whole occurrence.
            to_hide = skp.whole_sketches(to_hide)

            # Hide logic (robust)
            # A sketch or body hidden here is a DELIBERATE hide, and it has to be
            # recorded as one: the policy recomputes every leaf bulb from the
            # intent record, so a hide that only touched the bulb would be undone
            # by the next navigation.  Nothing reads the bulbs back to work this
            # out any more — the gesture has to say so, and this is where it does.
            keys = []
            for ent in to_hide:
                try:
                    # Direct visibility
                    # An occurrence's hide is recorded too (fvs.note_occ_intent), or
                    # restoring its level on the next navigation shows it again.
                    if hasattr(ent, 'isLightBulbOn'):
                        ent.isLightBulbOn = False
                        if isinstance(ent, adsk.fusion.Occurrence):
                            fvs.note_occ_intent(ent, False)

                    # If it's inside an occurrence (assembly context)
                    elif hasattr(ent, 'assemblyContext') and ent.assemblyContext:
                        ent.assemblyContext.isLightBulbOn = False
                        fvs.note_occ_intent(ent.assemblyContext, False)

                except:
                    pass
                try:
                    k = fvs.leaf_key_for(ent)
                    if k:
                        keys.append(k)
                except:
                    pass
            if keys:
                try:
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    if design:
                        fvs.set_leaf_intent(design, keys, True)
                except:
                    pass

            # Clear selection AFTER hiding
            selection.clear()

        except:
            if ui:
                ui.messageBox('Execution failed:\n{}'.format(traceback.format_exc()))


# ---------- COMMAND CREATED ----------
class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            cmd = args.command
            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)
        except:
            if ui:
                ui.messageBox('Command creation failed:\n{}'.format(traceback.format_exc()))


# ---------- RUN ----------
def run(context):
    try:
        cmd_defs = ui.commandDefinitions
        cmd_def = cmd_defs.itemById(CMD_ID)

        if not cmd_def:
            cmd_def = cmd_defs.addButtonDefinition(
                CMD_ID,
                CMD_NAME,
                CMD_DESC
            )

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        # Attach to a SAFE panel (Add-Ins panel)
        workspace = ui.workspaces.itemById('FusionSolidEnvironment')

        panel = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        if not panel:
            raise Exception('Could not find Add-Ins panel')

        control = panel.controls.itemById(CMD_ID)
        if not control:
            panel.controls.addCommand(cmd_def)

    except:
        if ui:
            ui.messageBox('Add-in start failed:\n{}'.format(traceback.format_exc()))


# ---------- STOP ----------
def stop(context):
    try:
        workspace = ui.workspaces.itemById('FusionSolidEnvironment')
        panel = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')

        if panel:
            control = panel.controls.itemById(CMD_ID)
            if control:
                control.deleteMe()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

    except:
        if ui:
            ui.messageBox('Add-in stop failed:\n{}'.format(traceback.format_exc()))