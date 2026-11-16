"""
+ Minimal UI
────────────
A toggle that strips Fusion down to a near-bare canvas and restores the normal
UI on the next press. It drives Fusion's own show/hide commands (the ones
behind the right-click "Hide Navigation Bar" etc.):

    HideCommandToolbarCommand     ← the ribbon ("Command Toolbar")
    HideNavigationToolbarCommand  ← the navigation bar
    HideBrowserCommand            ← the Browser
    HideViewCubeCommand           ← the ViewCube

Unlike hiding individual buttons, "Hide Command Toolbar" collapses the whole
ribbon and reclaims its vertical space.

NOT controllable via the API on this build (confirmed by dumping all 3150
command definitions — see "Dump Commands" in the Scripts folder):
    • the parametric Design *timeline* — no show/hide command or property
      exists. It's hidden only in environments that don't use it (Render,
      Animation, Manufacture); the API can't create a custom workspace nor
      toggle the timeline within Design.
    • the thin top application bar (file menu / save / open-document tabs /
      workspace switcher) — no hide command exists for it.

State is persisted, survives a Fusion restart, and is re-applied when documents
are activated (a new document brings the ribbon back).

IMPORTANT — in minimal mode the ribbon button that toggles this is gone too.
Assign a keyboard shortcut so you can always get back: right-click
"Toggle Minimal UI" in the ADD-INS panel (UTILITIES tab) ▸ "Change Keyboard
Shortcut". Shortcuts fire even when the chrome is hidden.
"""

import adsk.core
import json
import os
import threading
import traceback

_app = None
_ui = None
_handlers = []

CMD_ID   = 'JoshMinimalUITgl'
CMD_NAME = 'Toggle Minimal UI'
CMD_DESC = 'Hide/show the ribbon, navigation bar and browser for a bare canvas'

CUSTOM_EVENT_ID = 'JoshMinimalUIReapply'

# Fusion's built-in show/hide commands, paired (hide, show).
# NOTE: the ribbon (Command Toolbar) is intentionally NOT here — the "Maximize
# Viewport" add-in owns the ribbon (via its "Hide Ribbon + Tabs" toggle) so the
# two don't fight over it. Minimal UI handles nav bar / browser / ViewCube only.
CHROME_CMDS = [
    ('HideNavigationToolbarCommand', 'ShowNavigationToolbarCommand'),  # nav bar
    ('HideBrowserCommand',           'ShowBrowserCommand'),            # browser
    ('HideViewCubeCommand',          'ShowViewCubeCommand'),           # ViewCube
]

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'Minimal UI State.json')

# Shared with the Browser add-in (fvs._SHARED_STATE_FILE / Shared State.py): one level
# up, at the AddIns root. Read directly rather than importing fvs — all this needs is
# two flags, not the whole visibility engine.
_SHARED_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Shared State.json')


def _browser_is_native_and_shown():
    """True when `y` (BrowserSwapNativeCmd) has chosen Fusion's own browser and it is
    currently shown — in which case Minimal UI must leave HideBrowserCommand /
    ShowBrowserCommand alone, or every document activation hides the browser the user
    just swapped to."""
    try:
        with open(_SHARED_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return (isinstance(data, dict) and data.get('browser_kind') == 'native'
                and bool(data.get('native_browser_shown')))
    except Exception:
        return False


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


def _is_minimal():
    return bool(_read_state().get('minimal', False))


# ── chrome show/hide ──────────────────────────────────────────────────────────

def _exec(cmd_id):
    cmd_def = _ui.commandDefinitions.itemById(cmd_id)
    if cmd_def:
        try:
            cmd_def.execute()
        except Exception:
            pass


def _sync_chrome():
    """Hide or show the chrome to match the persisted state. Runs from the
    custom event so no Fusion command is active (commands are ignored if
    .execute() is called while another command is running)."""
    minimal = _is_minimal()
    native_shown = _browser_is_native_and_shown()
    for hide_id, show_id in CHROME_CMDS:
        if hide_id == 'HideBrowserCommand' and native_shown:
            continue    # `y` chose Fusion's browser and it's up — leave it alone
        _exec(hide_id if minimal else show_id)


def _set_checked(value):
    cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        try:
            cmd_def.isChecked = value
        except Exception:
            pass


def _fire_soon(delay=0.3):
    def fire():
        threading.Event().wait(delay)
        try:
            _app.fireCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass
    threading.Thread(target=fire, daemon=True).start()


# ── toggle ──────────────────────────────────────────────────────────────────

def _toggle():
    new_state = not _is_minimal()
    _write_state({'minimal': new_state})
    _set_checked(new_state)
    _fire_soon()   # apply outside the command-execute context


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


class ReapplyEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            _sync_chrome()
        except Exception:
            pass


class DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    """A freshly activated document brings the ribbon back; re-hide if minimal."""
    def notify(self, args):
        if _is_minimal():
            _fire_soon(1.5)


# ── lifecycle ─────────────────────────────────────────────────────────────────

def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface

    try:
        existing = _ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()

        try:
            cmd_def = _ui.commandDefinitions.addCheckBoxDefinition(
                CMD_ID, CMD_NAME, CMD_DESC, _is_minimal())
        except Exception:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)

        on_created = ToggleCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        ws = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')

        # A control left behind by a prior load makes addCommand raise
        # InternalValidationError — clear it first.
        old_ctrl = panel.controls.itemById(CMD_ID)
        if old_ctrl:
            old_ctrl.deleteMe()

        ctrl = panel.controls.addCommand(cmd_def)
        if ctrl:
            ctrl.isPromoted = True

        try:
            custom_event = _app.registerCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            _app.unregisterCustomEvent(CUSTOM_EVENT_ID)
            custom_event = _app.registerCustomEvent(CUSTOM_EVENT_ID)
        reapply_handler = ReapplyEventHandler()
        custom_event.add(reapply_handler)
        _handlers.append((custom_event, reapply_handler))

        doc_handler = DocumentActivatedHandler()
        _app.documentActivated.add(doc_handler)
        _handlers.append((_app.documentActivated, doc_handler))

        # Force minimal mode ON whenever Fusion opens, regardless of the
        # state Fusion was last closed in.
        _write_state({'minimal': True})
        _set_checked(True)
        _fire_soon()

    except Exception:
        if _ui:
            _ui.messageBox('Minimal UI start failed:\n{}'.format(
                traceback.format_exc()))


def stop(context):
    try:
        # Always leave the UI usable (full) when the add-in stops.
        for _hide_id, show_id in CHROME_CMDS:
            _exec(show_id)
        _write_state({'minimal': False})

        ws = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        try:
            _app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass

    except Exception:
        if _ui:
            _ui.messageBox('Minimal UI stop failed:\n{}'.format(
                traceback.format_exc()))
