"""
Appearance Toggle — light/dark viewport appearance.

Dark mode uses Josh's "Dark Sky" environment; light mode uses "Tranquility Blue"
(TB = Dark Sky with mid-grey unconstrained lines). Two toolbar buttons:
  • "Appearance: Light/Dark"  — flip the mode now (turns auto-follow off).
  • "Appearance: Follow macOS" — when on, the mode tracks the macOS system
    appearance (which itself flips at sunset if "Auto" is set in System Settings).

The chosen mode is written to a shared file so Browser's environment backstop and
the companion windows (Params/Browser) can read it. Environment is applied on the
Fusion main thread via a custom event (the poller runs on a background thread).

Follows the reload/shortcut-safe add-in pattern: find-or-create in run(),
handlers.clear() (no deleteMe) in stop(), guard handlers with a module flag.
"""

import adsk.core, adsk.fusion
import json, os, subprocess, threading, traceback

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

PANEL_ID  = 'SolidScriptsAddinsPanel'
EVENT_ID  = 'AppearanceApplyEvent'

TOGGLE_CMD_ID = 'AppearanceToggleCmd'
FOLLOW_CMD_ID = 'AppearanceFollowCmd'

# Shared state, read by Browser's environment backstop and the companion windows.
# Lives with the rest of the shared Fusion-UI config.
STATE_FILE = '/Users/josh/Code/Scripts/fusion_ui/appearance.json'

# Logical mode -> base environment display name.
ENV_FOR = {'dark': 'Dark Sky', 'light': 'Tranquility Blue'}

_active     = False
_custom_evt = None
_stop_evt   = threading.Event()
_apply_target = None   # env name the main-thread handler should apply, or None


# ── Shared state ────────────────────────────────────────────────────────────────

def _read_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        return {'mode': d.get('mode', 'dark'), 'follow_macos': bool(d.get('follow_macos', False))}
    except Exception:
        return {'mode': 'dark', 'follow_macos': False}


def _write_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _macos_is_dark():
    """True if macOS is in Dark appearance (the key is absent in Light mode)."""
    try:
        out = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                             capture_output=True, text=True, timeout=2)
        return out.returncode == 0 and 'dark' in out.stdout.strip().lower()
    except Exception:
        return None   # unknown — don't force anything


# ── Environment switching (main thread only) ────────────────────────────────────

def set_environment(env_name):
    """Select an environment by display name in the View > Environment list.

    .strip(): the items sit under the "Theme Environments" / "Legacy Environments"
    headers and carry that indentation in .name — the item is called '    Dark Sky'.
    An exact == matched nothing, so no environment switch anywhere ever took effect.
    """
    try:
        cmd_def = ui.commandDefinitions.itemById('ViewEnvCommand')
        if cmd_def:
            ctl = cmd_def.controlDefinition
            lcd = adsk.core.ListControlDefinition.cast(ctl)
            if lcd:
                # isEnabled dance: the Render workspace disables ViewEnvCommand and a
                # selection assigned while disabled never reaches the viewport.
                was = ctl.isEnabled
                try:
                    if not was:
                        ctl.isEnabled = True
                    for item in lcd.listItems:
                        if item.name.strip() == env_name:
                            item.isSelected = True
                            return True
                finally:
                    if not was:
                        try:
                            ctl.isEnabled = False
                        except Exception:
                            pass
    except Exception:
        pass
    try:
        for cd in ui.commandDefinitions:
            if cd.name.strip() == env_name:
                cd.execute()
                return True
    except Exception:
        pass
    return False


def apply_mode(mode):
    """Persist the mode and apply its base environment on the main thread."""
    st = _read_state()
    st['mode'] = mode
    _write_state(st)
    set_environment(ENV_FOR.get(mode, 'Dark Sky'))


# ── Background follow-poller ─────────────────────────────────────────────────────

def _poller():
    """When follow_macos is on, request the mode that matches macOS appearance."""
    global _apply_target
    while not _stop_evt.is_set():
        try:
            st = _read_state()
            if st.get('follow_macos'):
                dark = _macos_is_dark()
                if dark is not None:
                    want = 'dark' if dark else 'light'
                    if want != st.get('mode'):
                        st['mode'] = want
                        _write_state(st)
                        _apply_target = ENV_FOR.get(want, 'Dark Sky')
                        app.fireCustomEvent(EVENT_ID, '')
        except Exception:
            pass
        _stop_evt.wait(4.0)


# ── Lifecycle ────────────────────────────────────────────────────────────────────

def run(context):
    global _active, _custom_evt
    try:
        _active = True
        _stop_evt.clear()

        _custom_evt = app.registerCustomEvent(EVENT_ID)
        he = ApplyHandler()
        _custom_evt.add(he)
        handlers.append(he)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid, name, desc in (
            (TOGGLE_CMD_ID, 'Appearance: Light/Dark', 'Toggle the viewport between light and dark appearance'),
            (FOLLOW_CMD_ID, 'Appearance: Follow macOS', 'Track the macOS system appearance (flips at sunset with Auto)'),
        ):
            cd = ui.commandDefinitions.itemById(cid)
            if not cd:
                cd = ui.commandDefinitions.addButtonDefinition(cid, name, desc)
            h = CommandCreatedHandler()
            cd.commandCreated.add(h)
            handlers.append(h)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(cd)

        threading.Thread(target=_poller, daemon=True).start()
        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Appearance Toggle failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _active, _custom_evt
    try:
        _active = False
        _stop_evt.set()
        handlers.clear()
        if _custom_evt:
            try:
                app.unregisterCustomEvent(EVENT_ID)
            except Exception:
                pass
            _custom_evt = None
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cid in (TOGGLE_CMD_ID, FOLLOW_CMD_ID):
                ctrl = panel.controls.itemById(cid)
                if ctrl:
                    ctrl.deleteMe()
    except Exception:
        pass


# ── Handlers ─────────────────────────────────────────────────────────────────────

class ApplyHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _apply_target
        if not _active:
            return
        try:
            tgt = _apply_target
            _apply_target = None
            if tgt:
                set_environment(tgt)
        except Exception:
            pass


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = CommandExecuteHandler(args.command.parentCommandDefinition.id)
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, cmd_id):
        super().__init__()
        self.cmd_id = cmd_id

    def notify(self, args):
        try:
            st = _read_state()
            if self.cmd_id == TOGGLE_CMD_ID:
                # Manual flip wins over auto-follow → turn follow off.
                st['follow_macos'] = False
                st['mode'] = 'light' if st.get('mode') == 'dark' else 'dark'
                _write_state(st)
                set_environment(ENV_FOR.get(st['mode'], 'Dark Sky'))
            elif self.cmd_id == FOLLOW_CMD_ID:
                st['follow_macos'] = not st.get('follow_macos', False)
                _write_state(st)
                if st['follow_macos']:
                    dark = _macos_is_dark()
                    if dark is not None:
                        st['mode'] = 'dark' if dark else 'light'
                        _write_state(st)
                        set_environment(ENV_FOR.get(st['mode'], 'Dark Sky'))
                ui.messageBox('Follow macOS appearance: ' + ('ON' if st['follow_macos'] else 'OFF'))
        except Exception:
            if ui:
                ui.messageBox('Appearance toggle failed:\n{}'.format(traceback.format_exc()))
