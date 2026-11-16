# Explode
#
# Dissolves the selected component occurrence one level:
#   - Every direct child occurrence moves into the parent component.
#   - Every body in the component is copied into the parent component,
#     transformed so it stays at exactly the same position in world space.
#   - The now-empty occurrence (and its component definition, if unreferenced)
#     is deleted.
#
# World-space positions are preserved for both bodies and sub-occurrences.
# The entire operation is a single undo step because it runs inside a
# CommandExecuteHandler.
#
# Sketches and construction geometry inside the exploded component are NOT
# moved — Fusion 360's API does not expose a reliable way to re-parent them.
# You will see a warning if any are present.
#
# UI LOCATION: SOLID → ADD-INS dropdown

import adsk.core
import adsk.fusion
import traceback

app      = adsk.core.Application.get()
ui       = app.userInterface
handlers = []

CMD_ID   = 'ExplodeComponentAddin'
CMD_NAME = 'Explode'
CMD_DESC = ('Dissolves the selected component one level, moving its '
            'bodies and sub-components into the parent while preserving '
            'world-space positions.')


# =========================================================
# CORE LOGIC
# =========================================================

def explode_occurrence(occ: adsk.fusion.Occurrence):
    design = adsk.fusion.Design.cast(app.activeProduct)
    comp   = occ.component

    if comp is design.rootComponent:
        ui.messageBox('Cannot explode the root component.')
        return

    T_occ = occ.transform

    parent_comp = (occ.assemblyContext.component
                   if occ.assemblyContext
                   else design.rootComponent)

    # Warn about sketches / construction geometry we cannot move
    unmovable = (comp.sketches.count
                 + comp.constructionPlanes.count
                 + comp.constructionAxes.count
                 + comp.constructionPoints.count)
    if unmovable > 0:
        ui.messageBox(
            f'"{comp.name}" contains {unmovable} sketch(es) or construction '
            'geometry item(s) that will be left behind after the explosion.\n'
            'Only bodies and component occurrences will be moved.',
            CMD_NAME,
            adsk.core.MessageBoxButtonTypes.OKButtonType,
            adsk.core.MessageBoxIconTypes.InformationIconType,
        )

    # ------------------------------------------------------------------
    # 1. Re-parent each direct child occurrence into the parent component
    # ------------------------------------------------------------------
    # Pre-compute ALL (component_def, T_new) pairs BEFORE touching the design.
    # Reading child.transform inside the loop after addExistingComponent calls
    # can return stale values as Fusion updates the occurrence list.
    child_plan = []
    for i in range(comp.occurrences.count):
        child   = comp.occurrences.item(i)
        T_new   = T_occ.copy()
        T_new.transformBy(child.transform)
        child_plan.append((child.component, T_new))

    for comp_def, T_new in child_plan:
        new_occ = parent_comp.occurrences.addExistingComponent(comp_def, T_new)
        new_occ.transform = T_new  # force — addExistingComponent ignores identity matrix

    # ------------------------------------------------------------------
    # 2. Copy bodies into the parent, repositioned to parent space
    # ------------------------------------------------------------------
    bodies = [comp.bRepBodies.item(i) for i in range(comp.bRepBodies.count)]

    if bodies:
        temp_mgr  = adsk.fusion.TemporaryBRepManager.get()
        base_feat = parent_comp.features.baseFeatures.add()
        base_feat.startEdit()
        try:
            for body in bodies:
                tmp      = temp_mgr.copy(body)
                temp_mgr.transform(tmp, T_occ)
                new_body = parent_comp.bRepBodies.add(tmp, base_feat)
                new_body.name = body.name
        finally:
            base_feat.finishEdit()

    # ------------------------------------------------------------------
    # 3. Delete the occurrence; clean up the component definition if unused
    # ------------------------------------------------------------------
    comp_name = comp.name
    occ.deleteMe()

    comp_def = design.allComponents.itemByName(comp_name)
    if comp_def:
        remaining = design.rootComponent.allOccurrencesByComponent(comp_def)
        if remaining.count == 0:
            try:
                comp_def.deleteMe()
            except Exception:
                pass


# =========================================================
# COMMAND HANDLERS
# =========================================================

class CommandExecuteHandler(adsk.core.CommandEventHandler):

    def __init__(self, occ_path):
        super().__init__()
        self._occ_path = occ_path

    def notify(self, args):
        try:
            if not self._occ_path:
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            occ    = None
            for o in design.rootComponent.allOccurrences:
                if o.fullPathName == self._occ_path:
                    occ = o
                    break
            if occ is None:
                return
            explode_occurrence(occ)
        except Exception:
            ui.messageBox('Explode failed:\n' + traceback.format_exc(), CMD_NAME)


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):

    def __init__(self):
        super().__init__()

    def notify(self, args):
        if self not in handlers:
            return
        try:
            # Capture full path now — Fusion clears activeSelections before execute fires.
            occ_path = None
            for i in range(ui.activeSelections.count):
                entity    = ui.activeSelections.item(i).entity
                candidate = adsk.fusion.Occurrence.cast(entity)
                if candidate:
                    occ_path = candidate.fullPathName
                    break

            on_execute = CommandExecuteHandler(occ_path)
            args.command.execute.add(on_execute)
            handlers.append(on_execute)
        except Exception:
            ui.messageBox('CommandCreated failed:\n' + traceback.format_exc(), CMD_NAME)


# =========================================================
# START / STOP
# =========================================================

def run(context):
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        workspace = ui.workspaces.itemById('FusionSolidEnvironment')
        panel     = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        if not panel.controls.itemById(CMD_ID):
            control = panel.controls.addCommand(cmd_def)
            control.isPromoted          = False
            control.isPromotedByDefault = False

    except Exception:
        ui.messageBox('Explode – start failed:\n' + traceback.format_exc())


def stop(context):
    try:
        handlers.clear()
        workspace = ui.workspaces.itemById('FusionSolidEnvironment')
        panel     = workspace.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
    except Exception:
        pass
