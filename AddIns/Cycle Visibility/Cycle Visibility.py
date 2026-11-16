"""
Cycle Visibility
────────────────
One command for the two transient visibility flags (see fvs.cycle_visibility):

    bodies only → both on → sketches only → bodies only   (arriving in a component
    starts at bodies only — its own, or the parent's via the fallback below; only
    where neither has a body does it start at both on)
    in a sketch edit:  neither → bodies only → sketches only → neither
                       (neither ↔ bodies only when the sketch has no sibling sketches)

Sketches only ever show for the active component itself (never its children's)
unless isolation is off.

Bodies with nothing visible in the active subtree fall back one level up: the
parent's own and its other children's bodies show instead, and isolation widens
onto the parent for as long as that lasts. Sketches never fall back. Outside a
sketch, a step that would show nothing (e.g. sketches only in a component with no
sketches of its own) is skipped.

A NEW sketch (Sketch: New) opens at whatever position the cycle was at when the
command ran, instead of the sketch-edit default (fvs § New sketch keeps the cycle
position); opening an existing sketch still starts at neither.

Cycle Visibility (Reverse) walks the same cycles backwards
(fvs.cycle_visibility(reverse=True)).

Same model as Toggle Sketch / Toggle Body Visibility (which stay installed for the
Browser's buttons): the state is per activation context and reverts to the
depth-based default when the active component changes.
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
fvs = _load('fvs', 'Visibility Snapshot.py')

CMD_ID   = 'CycleVisibilityCmd'
CMD_NAME = 'Cycle Visibility'
CMD_DESC = 'Cycle: bodies only → sketches + bodies → sketches only (a sketch adds neither).'

REV_ID   = 'CycleVisibilityReverseCmd'
REV_NAME = 'Cycle Visibility (Reverse)'
REV_DESC = 'Cycle backwards: bodies only → sketches only → sketches + bodies (a sketch adds neither).'

# (id, name, description, reverse)
_COMMANDS = ((CMD_ID, CMD_NAME, CMD_DESC, False),
             (REV_ID, REV_NAME, REV_DESC, True))

app = None
ui = None
handlers = []


class _OnExecute(adsk.core.CommandEventHandler):
    def __init__(self, reverse):
        super().__init__()
        self.reverse = reverse

    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                fvs.cycle_visibility(design, reverse=self.reverse)
        except Exception:
            ui.messageBox('CycleVisibility – Execute error:\n' + traceback.format_exc())


class _OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def __init__(self, reverse):
        super().__init__()
        self.reverse = reverse

    def notify(self, args):
        try:
            on_exec = _OnExecute(self.reverse)
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
        except Exception:
            ui.messageBox('CycleVisibility – CommandCreated error:\n' + traceback.format_exc())


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        for cmd_id, name, desc, reverse in _COMMANDS:
            existing = ui.commandDefinitions.itemById(cmd_id)
            if existing:
                existing.deleteMe()
            cmd_def = ui.commandDefinitions.addButtonDefinition(cmd_id, name, desc)

            on_created = _OnCommandCreated(reverse)
            cmd_def.commandCreated.add(on_created)
            handlers.append(on_created)

            if panel and not panel.controls.itemById(cmd_id):
                panel.controls.addCommand(cmd_def)
    except Exception:
        if ui:
            ui.messageBox('CycleVisibility – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        for cmd_id, _name, _desc, _reverse in _COMMANDS:
            if panel:
                ctrl = panel.controls.itemById(cmd_id)
                if ctrl:
                    ctrl.deleteMe()
            cmd_def = ui.commandDefinitions.itemById(cmd_id)
            if cmd_def:
                cmd_def.deleteMe()
        handlers.clear()
    except Exception:
        if ui:
            ui.messageBox('CycleVisibility – stop error:\n' + traceback.format_exc())
