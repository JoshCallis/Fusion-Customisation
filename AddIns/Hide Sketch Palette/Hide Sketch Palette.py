"""
+ Hide Sketch Palette
─────────────────────
The Sketch Palette (grid/snap/slice/Look At options + Finish Sketch) is the
small panel Fusion plants at the top-right of the canvas every time a sketch
is opened for edit. It has no API to hide — confirmed live via AX: it is a
separate window with an empty title, distinct from the canvas, and none of
Sketch's own properties (areDimensionsShown etc.) touch its visibility.

So this shells out to bin/sketch_palette (~/bin/sketch_palette — see that
source for how it's identified and moved) after every SketchActivate /
SketchCreate, positioning the palette window off-screen and holding it there
for a settle window (Fusion places it late). It is still a live window off
there — Finish Sketch, Snap and the rest keep working exactly as if it were
on-screen — it is just out of the way.

Any command run inside a sketch (Dimension, Line, …) makes Fusion re-show the
palette beside its dialog, so the helper also holds it off-screen from that
command's commandStarting until it terminates. Moving alone still let it flash
for a frame each time, so the palette's NSWindow is also made transparent and
click-through from in here (the helper reports its window number) — Fusion can
put it back wherever it likes and nothing shows.

"Toggle Sketch Palette" (ADD-INS panel ▸ UTILITIES tab) turns this off and
puts the palette straight back; while off, it appears normally. Every Fusion launch starts ON (palette hidden).
"""
import adsk.core
import ctypes
import ctypes.util
import json
import os
import subprocess
import threading
import traceback

_app = None
_ui = None
_handlers = []

CMD_ID   = 'JoshHideSketchPaletteTgl'
CMD_NAME = 'Toggle Sketch Palette'
CMD_DESC = ('Hide the Sketch Palette off-screen whenever a sketch is opened '
            'for edit')

HELPER = '/Users/josh/bin/sketch_palette'

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'Hide Sketch Palette State.json')


# ── state persistence ───────────────────────────────────────────────────────

def _read_state():
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data):
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _hiding_enabled():
    return bool(_read_state().get('hidden', True))   # default ON


def _set_checked(value):
    cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        try:
            cmd_def.isChecked = value
        except Exception:
            pass


# ── the move itself ────────────────────────────────────────────────────────

# Fusion places the palette some time AFTER SketchActivate/SketchCreate
# terminate — a single hide fired 150 ms later raced that and usually lost,
# which is why it used to take several goes. So the helper now keeps watching
# for a settle window and re-hides whenever Fusion puts the palette back.
ENTRY_SETTLE_MS = 2500   # after opening a sketch for edit
OTHER_SETTLE_MS = 800    # after any other command while in a sketch
# A command dialog (Dimension, Line, …) makes Fusion put the palette back at its
# home spot for as long as the dialog is up. So from commandStarting until that
# command terminates the helper holds it off-screen; the terminate handler then
# replaces the hold with a normal settle. The cap only matters if a terminate
# never arrives (a command vetoed in its own commandStarting).
COMMAND_HOLD_MS = 120000

_proc = None             # the running helper, if any
_proc_lock = threading.Lock()


# ── the veil: palette transparent + click-through, in-process ───────────────

VEIL_EVENT = 'JoshHideSketchPaletteVeil'
_veiled = set()          # window numbers made transparent, so 'show' can undo

_objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]


def _msg(restype, *argtypes):
    # Private prototypes — never set restype on the shared cdll.objc_msgSend.
    return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                            *argtypes)(('objc_msgSend', _objc))


_send_id      = _msg(ctypes.c_void_p)
_send_id_long = _msg(ctypes.c_void_p, ctypes.c_long)
_send_double  = _msg(None, ctypes.c_double)
_send_bool    = _msg(None, ctypes.c_bool)


def _sel(name):
    return _objc.sel_registerName(name)


def _veil(wid, on):
    """Main thread only (AppKit)."""
    nsapp = _send_id(_objc.objc_getClass(b'NSApplication'), _sel(b'sharedApplication'))
    win = _send_id_long(nsapp, _sel(b'windowWithWindowNumber:'), wid)
    if not win:
        return
    _send_double(win, _sel(b'setAlphaValue:'), 0.0 if on else 1.0)
    _send_bool(win, _sel(b'setIgnoresMouseEvents:'), on)


def _unveil_all():
    for wid in list(_veiled):
        try:
            _veil(wid, False)
        except Exception:
            pass
    _veiled.clear()


def _read_window_ids(proc):
    """Helper's stdout → the main thread, via a custom event."""
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.isdigit():
                _app.fireCustomEvent(VEIL_EVENT, line)
    except Exception:
        pass


class VeilHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            if not _hiding_enabled():
                return
            wid = int(args.additionalInfo)
            _veil(wid, True)
            _veiled.add(wid)
        except Exception:
            pass


def _run_helper(*args, replace=True):
    """Start the helper. replace=False leaves an in-flight settle loop alone
    (it's already doing the job); replace=True kills it first — needed so a
    'show' isn't immediately undone by a still-running 'hide'."""
    global _proc
    with _proc_lock:
        running = _proc is not None and _proc.poll() is None
        if running and not replace:
            return
        if running:
            try:
                _proc.kill()
            except Exception:
                pass
        hiding = args[0] == 'hide'
        try:
            _proc = subprocess.Popen([HELPER] + [str(a) for a in args],
                                     stdout=subprocess.PIPE if hiding else subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, text=True,
                                     # posix_spawn instead of forking all of Fusion:
                                     # ~0.3 ms vs ~60 ms, and this runs on every
                                     # command start/end inside a sketch (measured
                                     # 2026-09-26: ~6 spawns, ~360 ms per exit).
                                     close_fds=False)
        except Exception:
            _proc = None
            return
        if hiding:
            threading.Thread(target=_read_window_ids, args=(_proc,),
                             daemon=True).start()


def _editing_sketch():
    try:
        obj = _app.activeEditObject
        return obj is not None and obj.objectType == 'adsk::fusion::Sketch'
    except Exception:
        return False


def _another_command_running(ending_id):
    try:
        active = _ui.activeCommand
        return bool(active) and active not in ('SelectCommand', ending_id)
    except Exception:
        return False


def _toggle():
    new_state = not _hiding_enabled()
    _write_state({'hidden': new_state})
    _set_checked(new_state)
    if new_state:
        _run_helper('hide', ENTRY_SETTLE_MS)
    else:
        _unveil_all()
        _run_helper('show')   # put it back now, not on the next sketch


# ── handlers ──────────────────────────────────────────────────────────────────

class ToggleExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _toggle()
        except Exception:
            if _ui:
                _ui.messageBox(traceback.format_exc())


class ToggleCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        handler = ToggleExecuteHandler()
        args.command.execute.add(handler)
        _handlers.append(handler)


# The palette window is created fresh by whichever command actually opens a
# sketch for edit — SketchActivate for an existing sketch (double-click, the
# Browser, Dimension Value's own re-entry), SketchCreate for a brand-new one
# (Create Sketch). Confirmed live by logging every commandTerminated id through
# both paths — SketchActivate alone missed SketchCreate entirely.
SKETCH_EDIT_COMMANDS = frozenset(('SketchActivate', 'SketchCreate'))


class SketchEditTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if args.commandId == CMD_ID or not _hiding_enabled():
                return
            if args.commandId in SKETCH_EDIT_COMMANDS:
                _run_helper('hide', ENTRY_SETTLE_MS)
            elif _editing_sketch():
                # A replacement command starts BEFORE the old one terminates
                # (Smart Dimension's restart does this) — keep holding for it.
                if _another_command_running(args.commandId):
                    _run_helper('hide', COMMAND_HOLD_MS)
                else:
                    # Ends the hold; the settle catches Fusion re-placing the
                    # palette as the dialog closes.
                    _run_helper('hide', OTHER_SETTLE_MS)
        except Exception:
            pass


class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Hold the palette off-screen for the life of any command run inside a
    sketch — Fusion re-shows it alongside the command's dialog."""
    def notify(self, args):
        try:
            if args.commandId == CMD_ID or not _hiding_enabled():
                return
            if args.commandId in SKETCH_EDIT_COMMANDS or not _editing_sketch():
                return
            _run_helper('hide', COMMAND_HOLD_MS)
        except Exception:
            pass


# ── lifecycle ─────────────────────────────────────────────────────────────────

def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface

    try:
        # Every Fusion launch starts with hiding ON (palette hidden); the
        # toggle only lasts for the session. A manual add-in reload keeps it.
        if (context or {}).get('IsApplicationStartup', False):
            _write_state({'hidden': True})

        existing = _ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()

        try:
            cmd_def = _ui.commandDefinitions.addCheckBoxDefinition(
                CMD_ID, CMD_NAME, CMD_DESC, _hiding_enabled())
        except Exception:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)

        on_created = ToggleCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        ws = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')

        old_ctrl = panel.controls.itemById(CMD_ID)
        if old_ctrl:
            old_ctrl.deleteMe()
        panel.controls.addCommand(cmd_def)

        on_terminated = SketchEditTerminatedHandler()
        _ui.commandTerminated.add(on_terminated)
        _handlers.append(on_terminated)

        on_starting = CommandStartingHandler()
        _ui.commandStarting.add(on_starting)
        _handlers.append(on_starting)

        _app.unregisterCustomEvent(VEIL_EVENT)
        veil_event = _app.registerCustomEvent(VEIL_EVENT)
        on_veil = VeilHandler()
        veil_event.add(on_veil)
        _handlers.append(on_veil)

        if _hiding_enabled() and _editing_sketch():
            _run_helper('hide', ENTRY_SETTLE_MS)

    except Exception:
        if _ui:
            _ui.messageBox('Hide Sketch Palette start failed:\n{}'.format(
                traceback.format_exc()))


def stop(context):
    try:
        ws = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        for h in _handlers:
            for ev in (_ui.commandTerminated, _ui.commandStarting):
                try:
                    ev.remove(h)
                except Exception:
                    pass
        _handlers.clear()
        _app.unregisterCustomEvent(VEIL_EVENT)
        _run_helper('hide', 0)   # kill any hold still running
        _unveil_all()            # it stays off-screen; just not invisible forever

    except Exception:
        if _ui:
            _ui.messageBox('Hide Sketch Palette stop failed:\n{}'.format(
                traceback.format_exc()))
