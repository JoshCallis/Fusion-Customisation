"""
Project Bounds — put the edited sketch's parent-component origin point and bounds
rectangle into it, on demand. No dialog: it acts straight on the active sketch.

Auto-Project Origin already does the origin-and-bounds part automatically for every
sketch AS it is created (see that add-in). This is the same act, run by hand, for a
sketch that does not have them: one made before Auto-Project Origin existed, one made
through some other route that skipped it, or one where the projections were since
deleted. Bounds.project_into skips anything already projected, so pressing this on a
sketch that already has its projections adds nothing a second time — it is safe to
press whether or not you are sure.

DOES NOTHING OUTSIDE A SKETCH
──────────────────────────────
There is nothing to project into if no sketch is open. The key is inert then, same as
Sketch Dimensions is with no sketch open.

VETOES ITSELF
─────────────
Like every hotkey here. Fusion runs one command at a time, and this key is pressed
while a sketch is open — letting it become the active command would close the sketch
it is meant to be projecting into. Refusing it in commandStarting and doing the work
there sidesteps that entirely (see command_starting_veto).
"""

import os
import subprocess
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
    # Without it there is no way to say WHICH sketch is open — see Sketch Dimensions,
    # which loads the same module for the same reason.
    fvs = None

try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None

CMD_ID = 'ProjectBoundsCmd'
CMD_NAME = 'Sketch: Project Bounds'
CMD_DESC = ("Project the edited sketch's parent-component origin and bounds rectangle "
            "into it. No dialog.")
PANEL_ID = 'SolidScriptsAddinsPanel'

LOG = '/tmp/project_bounds.log'
HUD_FLASH = '/Users/josh/bin/hud_flash'

_created = None
_starting = None


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


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


def _fail(message):
    _log(message)
    _hud('fail', message)


def _pressed():
    """Project the parent component's origin and bounds into the edited sketch.
    Main thread, inside commandStarting."""
    if fvs is None or bounds is None:
        _fail('Project Bounds: a helper module failed to load')
        return
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            _fail('Project Bounds: no design open')
            return
        if not fvs.is_editing_sketch(design):
            _fail('Project Bounds: no sketch open — nothing to project into')
            return
        sk = fvs.active_edit_sketch(design)
        if sk is None:
            _fail('Project Bounds: a sketch is open but could not be identified')
            return
        native = sk.nativeObject or sk
        problems = bounds.project_new_sketch(design, native)
        # Bounds of its own but no bounds sketches in reach: they sit LATER in the
        # timeline than this sketch, so editing it rolls them back and there is nothing
        # to project. project_into is silent about that (it runs on every new sketch),
        # but pressed by hand it must not flash a tick for an empty result.
        comp = native.parentComponent
        if (comp is not None and not bounds.bounds_sketches(comp)
                and bounds.has_own_bounds(design, comp)):
            problems.append("%s's bounds sketches are later in the timeline than %s — "
                            "move them before it" % (comp.name, native.name))
        _log('projected into', sk.name, ':', '; '.join(problems) if problems else 'ok')
        if problems:
            _hud('fail', 'Project Bounds:\n' + '\n'.join(problems))
        else:
            _hud('ok')
    except Exception:
        tb = traceback.format_exc()
        _log('failed:', tb.replace('\n', ' | '))
        _hud('fail', 'Project Bounds failed:\n' + tb)


class _HotkeyStarting(adsk.core.ApplicationCommandEventHandler):
    """Veto our own command and do the work here instead — see the module docstring."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if args.commandId != CMD_ID:
                return
            args.isCanceled = True
            _pressed()
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


def run(context):
    global _created, _starting
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)
        else:
            try:
                if cmd_def.name != CMD_NAME:
                    cmd_def.name = CMD_NAME
                cmd_def.tooltip = CMD_DESC
            except Exception:
                pass

        created = _CommandCreated()
        cmd_def.commandCreated.add(created)
        handlers.append(created)
        _created = created

        starting = _HotkeyStarting()
        ui.commandStarting.add(starting)
        handlers.append(starting)
        _starting = starting

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        ui.messageBox('Project Bounds run() failed:\n%s' % traceback.format_exc())


def stop(context):
    """Unregister BEFORE dropping the references — see _created."""
    global _created, _starting
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is not None and _created is not None:
            try:
                cmd_def.commandCreated.remove(_created)
            except Exception:
                pass
        if _starting is not None:
            try:
                ui.commandStarting.remove(_starting)
            except Exception:
                pass
        _created = None
        _starting = None

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
