"""
Sketch Dimensions — one key that strips the annotation off the sketch you are drawing
in, and a second key for projected geometry.

A sketch mid-construction is mostly annotation. Every dimension and constraint glyph you
place stays on screen, and by the time there are a dozen the geometry is behind them —
so this hides them for a moment, and shows them again when you want to read them.

The first key toggles dimensions, constraints and construction lines together:

    everything on ↔ everything off

The current look is read back off the sketch, not remembered: all three shown turns
them all off, and anything else (all off, or some mix a manual toggle left behind)
lands on "everything on", since that is always a safe place to land.

The second key flips the sketch's projected geometry on its own.

IT IS NOT A SETTING
───────────────────
The cycle lasts exactly as long as the sketch edit it was set in. Opening any sketch
turns dimensions, constraints and construction back ON, because that is what
fvs.show_sketch_aids already does on every sketch enter, and it is the right default:
you open a sketch to work on it, and you want to see what it is currently held by. So
the key is a look, not a preference — press it, glance at the shape, press it back, and
the next sketch you open is unaffected.

Within the edit, though, the look does have to be remembered — not as a preference, but
so that the OTHER add-ins stop undoing it. apply_policy forces all three annotations on
for the sketch being edited on every run, and plenty of things re-run it mid-edit (Cycle
Visibility runs it twice per press, the Browser on every navigation), so peeling the
annotations off and then cycling body/sketch visibility put them straight back. The look
is therefore handed to fvs.remember_sketch_aid_look, stamped with this sketch, and
cleared on the next sketch enter or exit.

Projected geometry is the exception: show_sketch_aids does not touch it, so it stays as
you left it in that sketch — the same as Fusion's own Sketch Palette checkbox.

IT DOES NOTHING OUTSIDE A SKETCH
────────────────────────────────
`areDimensionsShown` is the API's "displayed when the sketch is not active" knob, so
setting it on a sketch you are not in would leave that sketch's annotations floating in
the model afterwards — which is precisely what fvs.hide_sketch_aids exists to prevent on
exit. So with no sketch open the key is inert.

VETOES ITSELF
─────────────
Like every hotkey here. Fusion runs one command at a time, and this key is pressed while
a sketch is open — letting it become the active command would close the sketch it is
meant to be looking at. Refusing it in commandStarting and doing the work there sidesteps
that entirely (see command_starting_veto).
"""

import os
import time
import traceback
import types

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


try:
    fvs = _load('fvs', 'Visibility Snapshot.py')
except Exception:
    # Without it there is no way to say WHICH sketch is open — and answering that from
    # design.activeEditObject is the wrong way (it raises for a sketch nested in an
    # occurrence, which is most of them). So the key does nothing rather than the wrong
    # thing.
    fvs = None

# The ids predate what the commands now do. They are kept because the key bindings
# (Fusion's own, remap/bindings.json, fusion_ui/bindings.json) are keyed on them.
CMD_ID = 'ToggleSketchDimensionsCmd'
CMD_NAME = 'Sketch: Toggle Annotations'
CMD_DESC = ('Hide the dimensions, constraints and construction lines of the sketch '
            'being edited, or bring them all back. Opening any sketch shows them all '
            'again.')

CMD_ID_PROJECTED = 'ToggleSketchConstructionCmd'
CMD_NAME_PROJECTED = 'Sketch: Toggle References (Projected Geometry)'
CMD_DESC_PROJECTED = ('Hide the projected geometry of the sketch being edited, or '
                      'bring it back.')

PANEL_ID = 'SolidScriptsAddinsPanel'

# This runs WHILE a command is active, which is exactly when a message box would be an
# outrage and when the MCP bridge refuses to run. So it writes a line instead — the same
# thing Smart Dimension and Auto-Project Origin do, and for the same reason.
LOG = '/tmp/sketch_dimensions.log'

# Registered handlers, kept so stop() can take them off their events again. Clearing the
# list without unregistering frees the object while Fusion still holds a pointer to it,
# and the command then silently does nothing at all — no error, no dialog.
_created = None
_created_projected = None
_starting = None


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _active_sketch():
    """The sketch being edited, or None (and logged) if there isn't one to name."""
    if fvs is None:
        _log('no visibility module, so there is no sketch to ask about')
        return None
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return None
    if not fvs.is_editing_sketch(design):
        _log('no sketch open — nothing to toggle')
        return None
    sk = fvs.active_edit_sketch(design)
    if sk is None:
        # Editing something the recall could not name. Better to do nothing than to
        # guess at a sketch and annotate the wrong one.
        _log('a sketch is open but could not be identified')
    return sk


def _active_cmd():
    try:
        return ui.activeCommand
    except Exception:
        return '?'


def _pressed():
    """Step the edited sketch to its next look. Main thread, inside commandStarting."""
    try:
        sk = _active_sketch()
        if sk is None:
            return
        # All three shown → all off. Anything else (all off, or a mix a manual toggle
        # left behind) → all on, since that is always a safe place to land.
        shown = not (sk.areDimensionsShown and sk.areConstraintsShown
                     and sk.isConstructionGeometryShown)
        # Pressed mid-command (Line), it ended that command. Log the active command
        # around each write so the one that kills it is on record (/tmp/sketch_dimensions.log).
        _log('active before:', _active_cmd())
        sk.isConstructionGeometryShown = shown
        _log('  after construction:', _active_cmd())
        sk.areDimensionsShown = shown
        _log('  after dimensions:', _active_cmd())
        sk.areConstraintsShown = shown
        _log('  after constraints:', _active_cmd())
        # Tell the policy what we just set, or the next apply_policy in this edit
        # (Cycle Visibility runs two) forces all three back on — see
        # fvs.remember_sketch_aid_look.
        if fvs is not None:
            fvs.remember_sketch_aid_look(sk, shown, shown, shown)
        _log('all on' if shown else 'all off', 'for', sk.name)
    except Exception:
        _log('failed:', traceback.format_exc().replace('\n', ' | '))


def _pressed_projected():
    """Flip the edited sketch's projected-geometry visibility."""
    try:
        sk = _active_sketch()
        if sk is None:
            return
        shown = bool(sk.isProjectedGeometryShown)
        sk.isProjectedGeometryShown = not shown
        _log('projected', 'off' if shown else 'on', 'for', sk.name)
    except Exception:
        _log('failed:', traceback.format_exc().replace('\n', ' | '))


class _HotkeyStarting(adsk.core.ApplicationCommandEventHandler):
    """Veto our own command and do the work here instead — see the module docstring."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if args.commandId == CMD_ID:
                args.isCanceled = True
                _pressed()
            elif args.commandId == CMD_ID_PROJECTED:
                args.isCanceled = True
                _pressed_projected()
        except Exception:
            _log('veto failed:', traceback.format_exc().replace('\n', ' | '))


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    """Only ever reached if the veto above did not fire; harmless either way."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
        except Exception:
            pass


def _ensure_command(cmd_id, name, desc):
    """Find-or-create a button definition and put it on the panel, so both commands
    show up in Fusion's own keyboard-shortcut list."""
    cmd_def = ui.commandDefinitions.itemById(cmd_id)
    if cmd_def is None:
        # Three arguments, not four: passing '' as the resource folder makes a
        # definition that registers and reports isValid but will not launch.
        cmd_def = ui.commandDefinitions.addButtonDefinition(cmd_id, name, desc)
    else:
        # The id is kept for the life of the add-in so a key binding survives, which
        # means an existing definition keeps whatever name it was first given.
        try:
            if cmd_def.name != name:
                cmd_def.name = name
            cmd_def.tooltip = desc
        except Exception:
            pass

    created = _CommandCreated()
    cmd_def.commandCreated.add(created)
    handlers.append(created)

    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    if panel and not panel.controls.itemById(cmd_id):
        panel.controls.addCommand(cmd_def)
    return created


def run(context):
    global _created, _created_projected, _starting
    try:
        _created = _ensure_command(CMD_ID, CMD_NAME, CMD_DESC)
        _created_projected = _ensure_command(
            CMD_ID_PROJECTED, CMD_NAME_PROJECTED, CMD_DESC_PROJECTED)

        starting = _HotkeyStarting()
        ui.commandStarting.add(starting)
        handlers.append(starting)
        _starting = starting
    except Exception:
        ui.messageBox('Sketch Dimensions run() failed:\n%s' % traceback.format_exc())


def stop(context):
    """Unregister BEFORE dropping the references — see _created."""
    global _created, _created_projected, _starting
    try:
        for cmd_id, created in ((CMD_ID, _created),
                                 (CMD_ID_PROJECTED, _created_projected)):
            cmd_def = ui.commandDefinitions.itemById(cmd_id)
            if cmd_def is not None and created is not None:
                try:
                    cmd_def.commandCreated.remove(created)
                except Exception:
                    pass
        if _starting is not None:
            try:
                ui.commandStarting.remove(_starting)
            except Exception:
                pass
        _created = None
        _created_projected = None
        _starting = None

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cmd_id in (CMD_ID, CMD_ID_PROJECTED):
                ctrl = panel.controls.itemById(cmd_id)
                if ctrl:
                    ctrl.deleteMe()
        # The definitions themselves are left in place: deleting them would drop the
        # key bindings with them. See the reload/shortcut-safe pattern the other
        # add-ins use.
        handlers.clear()
    except Exception:
        pass
