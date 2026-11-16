"""
Add Bounds to Component — give a component that already exists the same six
parameters, record and three bounds sketches New Component gives a brand-new one.

Sometimes the component came first: it was made natively, imported, or made before
bounds existed at all, and only later does it want to be situational — a sketch inside
it projecting the envelope, dimensions measured FROM a face, "width" meaning something
here. This is that, aimed at the ACTIVE component rather than making a new one.

THE DIALOG IS THE SAME ONE
───────────────────────────
New Component's panel, in the same value window, minus the Name field — the component
keeps the name it already has. See that add-in's docstring for why the dialog lives
there at all: the bounds are expressions, and expressions want the situational
autocomplete already built for the value box.

ALREADY HAS BOUNDS? THEN IT REFUSES
───────────────────────────────────
With a HUD cross (click it for why), never a dialog, and nothing opens (Bounds.has_own_bounds — parameters, record or
sketches). Editing them is the Browser's Edit Bounds on the component's row: the same
panel, prefilled with the component's current expressions. Change a face, fill in one
that was left out, or blank one to take it away — whatever was built on a removed face
is rebased onto the other face of its axis. See Bounds.edit_bounds. Any face may be
left blank when bounds are first added, too (Bottom is by default); every axis keeps
at least one.

THE SKETCHES LAND AS EARLY AS THE COMPONENT ALLOWS
───────────────────────────────────────────────────
Not wherever the timeline marker happens to be. See Bounds.add_bounds_to_component and
earliest_component_position: the sketches go in at the start of the Sketches phase,
before every other sketch (or immediately after whichever feature created the
component, if that is later), so that geometry already inside it — and everything
modelled there from now on — can be dimensioned against them.
"""

import json
import os
import subprocess
import traceback
import types
import urllib.request

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []
_created = None
_rebuild_created = []

CMD_ID = 'AddBoundsToComponentCmd'
CMD_NAME = 'Component: Add Bounds'
CMD_DESC = ('Give the active component its own bounds — parameters, record and the '
            'three bounds sketches — without creating a new occurrence. Refuses if it '
            'already has some (edit those from the Browser).')
PANEL_ID = 'SolidScriptsAddinsPanel'

CMD_ID_REBUILD = 'RebuildBoundsCmd'
CMD_NAME_REBUILD = 'Component: Rebuild Bounds'
CMD_DESC_REBUILD = ('Join every bounds sketch that is still four loose points into one '
                    'rectangle — the way new ones are built. Dimensions, values and '
                    'projections are untouched.')

# The value window's server, owned by the Dimension Value add-in.
VALUE_URL = 'http://127.0.0.1:8734'


HUD_FLASH = '/Users/josh/bin/hud_flash'


def _hud(kind, message=None):
    """Tick on success; a cross that opens `message` if clicked. Never a dialog."""
    try:
        args = [HUD_FLASH, kind]
        if message:
            args.append(message)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


def _ask_for_panel():
    """Ask the value window to open the Add Bounds panel, aimed at the active
    component. Best effort, same as New Component's opener: if that add-in is not
    running there is nothing to open."""
    try:
        req = urllib.request.Request(
            VALUE_URL + '/cmd',
            data=json.dumps({'action': 'openaddbounds'}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if not _ask_for_panel():
                _hud('fail',
                     'The value window is not answering, so the Add Bounds panel '
                     'cannot open.\n\nIt is served by the Dimension Value add-in — '
                     'check that one is running.')
            # No inputs are added, so Fusion shows no dialog of its own: the panel IS
            # the dialog, and it is already on its way (or the cross above says why
            # not — which component and whether it already has bounds is decided on
            # the other end, since that is where the design is read; it HUDs too).
        except Exception:
            _hud('fail', 'Add Bounds to Component failed:\n%s' % traceback.format_exc())


class _RebuildCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'Bounds.py')
            m = types.ModuleType('bounds')
            m.__file__ = path
            with open(path, 'r', encoding='utf-8') as f:
                exec(compile(f.read(), path, 'exec'), m.__dict__)
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                return
            done, problems = m.rebuild_rectangles(design)
            if problems:
                _hud('fail', '\n'.join(problems))
            else:
                _hud('ok' if done else 'ok')
        except Exception:
            _hud('fail', 'Rebuild Bounds failed:\n%s' % traceback.format_exc())


def run(context):
    global _created
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)

        created = _CommandCreated()
        cmd_def.commandCreated.add(created)
        handlers.append(created)
        _created = created

        rdef = ui.commandDefinitions.itemById(CMD_ID_REBUILD)
        if rdef is None:
            rdef = ui.commandDefinitions.addButtonDefinition(
                CMD_ID_REBUILD, CMD_NAME_REBUILD, CMD_DESC_REBUILD)
        rcreated = _RebuildCreated()
        rdef.commandCreated.add(rcreated)
        handlers.append(rcreated)
        _rebuild_created[:] = [rcreated]

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
        if panel and not panel.controls.itemById(CMD_ID_REBUILD):
            panel.controls.addCommand(rdef)
    except Exception:
        ui.messageBox('Add Bounds to Component run() failed:\n%s' % traceback.format_exc())


def stop(context):
    """Unregister BEFORE dropping the references — see _created."""
    global _created
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is not None and _created is not None:
            try:
                cmd_def.commandCreated.remove(_created)
            except Exception:
                pass
        _created = None
        rdef = ui.commandDefinitions.itemById(CMD_ID_REBUILD)
        if rdef is not None:
            for h in _rebuild_created:
                try:
                    rdef.commandCreated.remove(h)
                except Exception:
                    pass
        del _rebuild_created[:]

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cid in (CMD_ID, CMD_ID_REBUILD):
                ctrl = panel.controls.itemById(cid)
                if ctrl:
                    ctrl.deleteMe()
        # The definition itself is left in place: deleting it would drop the key binding
        # with it. See the reload/shortcut-safe pattern the other add-ins use.
        handlers.clear()
    except Exception:
        pass
