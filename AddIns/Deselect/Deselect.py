import adsk.core
import adsk.fusion
import traceback

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

CMD_ID = 'deselectAllCmd'
CMD_NAME = 'Deselect All'
CMD_DESC = 'Clears all selected geometry'

cmdDef = None
panel = None

def run(context):
    global cmdDef, panel, handlers
    try:
        # Create command definition if it doesn't exist
        cmdDef = ui.commandDefinitions.itemById(CMD_ID)
        if not cmdDef:
            cmdDef = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        # Add the command created handler
        onCreated = CommandCreatedHandler()
        cmdDef.commandCreated.add(onCreated)
        handlers.append(onCreated)

        # Find Utilities panel in Solid workspace
        workspace = ui.workspaces.itemById('FusionSolidEnvironment')  # Solid workspace
        # Try known panel IDs
        possible_panels = ['SolidScriptsAddinsPanel', 'SolidUtilitiesPanel']
        for pid in possible_panels:
            try:
                panel = workspace.toolbarPanels.itemById(pid)
                if panel:
                    break
            except:
                continue

        if not panel:
            ui.messageBox('Cannot find Utilities panel. Panel ID may have changed.')
            return

        # Add the command to the panel if not already present
        if not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmdDef)

    except:
        ui.messageBox('Failed to start Add-In:\n{}'.format(traceback.format_exc()))

def stop(context):
    global cmdDef, panel
    try:
        # Remove command from panel
        if panel:
            control = panel.controls.itemById(CMD_ID)
            if control:
                control.deleteMe()
        # Delete command definition
        if cmdDef:
            cmdDef.deleteMe()
    except:
        pass

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            # Clear all selections immediately when command is run
            ui.activeSelections.clear()
        except:
            ui.messageBox('Failed to clear selection:\n{}'.format(traceback.format_exc()))