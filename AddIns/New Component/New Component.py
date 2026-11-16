"""
New Component — create a component that knows its own bounds.

Fusion's own New Component makes an empty thing with no sense of where it sits. This
one asks, up front, for the envelope the component is to be modelled inside, and then
sets that up completely: six bounds parameters of its own, the record that says which
parameter is which face, and the three bounds sketches.

That matters because everything situational downstream reads those bounds. A sketch
made inside the component projects its rectangle automatically (see Auto-Project
Origin); the value box knows which end of an axis a dimension is nearer, what "width"
means here, and what a dimension is measured FROM. A component made any other way has
none of it.

THE DIALOG IS THE VALUE WINDOW
──────────────────────────────
Not a Fusion command dialog. A component's bounds are EXPRESSIONS — "Width",
"HalfDepth", "Width - MembraneThickness" — so those fields want the situational
autocomplete that already exists in the value window: family resolution, the unit
filter, word-anchored matching. Rebuilding that elsewhere, or going without it, were
the two alternatives.

So this add-in owns nothing but the key. It asks the value window's server for the
component panel, exactly as the Browser asks it for the parameter editor, and the
Dimension Value add-in — which owns that server and its main-thread tick — arms the
session and applies the result. Everything the panel does lives in
fusion_value/index.html and Bounds.py.
"""

import json
import traceback
import urllib.request

import adsk.core

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []
# The commandCreated handler, kept so stop() can take it off the event again. Clearing
# the list without unregistering frees the object while Fusion still holds a pointer to
# it, and the command then silently does nothing at all — no error, no dialog.
_created = None

CMD_ID = 'NewComponentWithBoundsCmd'
CMD_NAME = 'Component: New'
CMD_DESC = ('Create a component with its own bounds — parameters, record and the three '
            'bounds sketches.')
PANEL_ID = 'SolidScriptsAddinsPanel'

# The value window's server, owned by the Dimension Value add-in.
VALUE_URL = 'http://127.0.0.1:8734'


def _ask_for_panel():
    """Ask the value window to open the New Component panel.

    The request is queued and served on Fusion's main thread at the other end, because
    arming a session touches the API. Best effort: if that add-in is not running there
    is nothing to open, and saying so plainly beats a traceback.
    """
    try:
        req = urllib.request.Request(
            VALUE_URL + '/cmd',
            data=json.dumps({'action': 'opencomponent'}).encode(),
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
                ui.messageBox(
                    'The value window is not answering, so the New Component panel '
                    'cannot open.\n\nIt is served by the Dimension Value add-in — '
                    'check that one is running.', CMD_NAME)
            # No inputs are added, so Fusion shows no dialog of its own: the panel IS
            # the dialog, and it is already on its way.
        except Exception:
            ui.messageBox('New Component failed:\n%s' % traceback.format_exc())


def run(context):
    global _created
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            # Three arguments, not four. Passing '' as the resource folder makes a
            # definition that registers and reports isValid but will not launch: the
            # command simply does nothing, with no error anywhere.
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)

        created = _CommandCreated()
        cmd_def.commandCreated.add(created)
        handlers.append(created)
        _created = created

        # A control is what puts the command in Fusion's own keyboard-shortcut list,
        # which is the only keymap in this setup.
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        ui.messageBox('New Component run() failed:\n%s' % traceback.format_exc())


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

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        # The definition itself is left in place: deleting it would drop the key binding
        # with it. See the reload/shortcut-safe pattern the other add-ins use.
        handlers.clear()
    except Exception:
        pass
