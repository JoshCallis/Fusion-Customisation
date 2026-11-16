"""
Smart Sketch — say where the sketch goes, in words.

Fusion's own New Sketch asks you to pick a face or a plane. Neither is what a sketch is
usually FOR: it is 100mm in from the side, or 18mm down from the top, and the plane only
exists because Fusion needs one. So this asks for that sentence instead —

    Width                    the side face
    Width 100 - Membrane     the side, less (100 - Membrane)
    Top 18                   18mm down from the top
    X                        the midline
    X 250                    250mm out from the midline, in +X

— and builds the construction plane, the sketch on it, and the tie between the two. The
plane is created with its light off and named after the line, because it is not a thing
to manage: it exists to carry that sketch, and the line that placed it can be opened
again from the Browser's menu on the sketch.

WHICH WORDS ARE ON OFFER depends on the component's own bounds. A symmetric axis has a
midline and a side (X, Width); an asymmetric one has two named faces (Back, Front). See
Bounds.datum_words — the vocabulary is a fact about the component, not a fixed list.

THE DIALOG IS THE VALUE WINDOW
──────────────────────────────
Everything after the datum word is an expression, so it wants the situational
autocomplete that already exists there — and the situation is exactly what the datum
word just named. So this add-in owns nothing but the key: it asks the value window's
server for the plane box, and the Dimension Value add-in — which owns that server and
its main-thread tick — arms the session and applies the result, exactly as New Component
does.
"""

import json
import traceback
import urllib.request

import adsk.core

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []
# Kept so stop() can take it off the event again. Clearing the list without
# unregistering frees the object while Fusion still holds a pointer to it, and the
# command then silently does nothing at all — no error, no dialog.
_created = None

CMD_ID = 'SmartSketchCmd'
CMD_NAME = 'Sketch: New'
CMD_DESC = ('Place a sketch by naming a datum and how far in from it — "Width 100", '
            '"Top 18", "X". The plane is made for it and hidden.')
PANEL_ID = 'SolidScriptsAddinsPanel'

# The value window's server, owned by the Dimension Value add-in.
VALUE_URL = 'http://127.0.0.1:8734'


def _ask_for_box():
    """Ask the value window to open the plane box.

    The request is queued and served on Fusion's main thread at the other end, because
    arming a session touches the API. Best effort: if that add-in is not running there
    is nothing to open, and saying so plainly beats a traceback.
    """
    try:
        req = urllib.request.Request(
            VALUE_URL + '/cmd',
            data=json.dumps({'action': 'opensketch'}).encode(),
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
            if not _ask_for_box():
                ui.messageBox(
                    'The value window is not answering, so the plane box cannot '
                    'open.\n\nIt is served by the Dimension Value add-in — check that '
                    'one is running.', CMD_NAME)
            # No inputs are added, so Fusion shows no dialog of its own: the box IS
            # the dialog, and it is already on its way.
        except Exception:
            ui.messageBox('Smart Sketch failed:\n%s' % traceback.format_exc())


def run(context):
    global _created
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            # Three arguments, not four. Passing '' as the resource folder makes a
            # definition that registers and reports isValid but will not launch.
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
        ui.messageBox('Smart Sketch run() failed:\n%s' % traceback.format_exc())


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
