# Refresh Edges — forces a full timeline rebuild (start -> end) so Fusion regenerates
# all edge tessellation. Use after the hidden-edge gate (Hide Tangent Edges) leaves a
# now-sharp edge stale-hidden: only a full roll clears Fusion's incremental tess cache;
# partial moves/edits don't. Reload/shortcut-safe per Josh's boilerplate.
import adsk.core, adsk.fusion, traceback

CMD_ID   = 'josh_refresh_edges'
CMD_NAME = 'Refresh Edges'
CMD_DESC = 'Rebuild the timeline (start to end) to regenerate edge display (un-stick wrongly-hidden edges).'
PANEL_ID = 'SolidScriptsAddinsPanel'   # the toolbar ADD-INS dropdown

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

def _refresh():
    des = adsk.fusion.Design.cast(app.activeProduct)
    if not des:
        ui.messageBox('Refresh Edges: no active design.')
        return
    tl = des.timeline
    if tl.count == 0:
        return
    orig = tl.markerPosition
    tl.markerPosition = 0
    adsk.doEvents()
    tl.markerPosition = tl.count
    adsk.doEvents()
    if 0 <= orig < tl.count:
        tl.markerPosition = orig
        adsk.doEvents()
    app.activeViewport.refresh()

class ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self): super().__init__()
    def notify(self, args):
        if self not in handlers:
            return
        try:
            _refresh()
        except Exception:
            ui.messageBox('Refresh Edges failed:\n' + traceback.format_exc())

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self): super().__init__()
    def notify(self, args):
        if self not in handlers:
            return
        try:
            eh = ExecuteHandler()
            args.command.execute.add(eh)
            handlers.append(eh)
        except Exception:
            ui.messageBox('Refresh Edges (create) failed:\n' + traceback.format_exc())

def run(context):
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        h = CommandCreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        if ui:
            ui.messageBox('Refresh Edges (run) failed:\n' + traceback.format_exc())

def stop(context):
    try:
        handlers.clear()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        # Do NOT delete the command definition (preserves any shortcut binding).
    except Exception:
        pass
