"""
Hide Native Timeline — add-in.

One checkbox command, Hide Native Timeline (ADD-INS panel; assign a hotkey), that
hides / shows Fusion's own Design timeline bar. Independent of the dock's vertical
timeline (AddIns/Timeline), which no longer touches the native bar — so both can be
on at once.

The hiding itself is Maximize Viewport's: it owns the Qt timeline widgets and the
canvas stretch. This add-in only holds the request, builtins._fusion_hide_native_timeline
(one copy per Fusion process), and fires MaximizeViewportReapply so it lands now
rather than on the next idle tick. Maximize Viewport ORs it with its own Hide
Timeline setting, so leave that one off and use this.

The state persists across restarts (Hide Native Timeline State.json). Stopping the
add-in drops the request, so the bar comes back.
"""

import adsk.core
import builtins
import json
import os
import traceback

_app = None
_ui = None
_handlers = []

CMD_ID = 'HideNativeTimelineTgl'
CMD_NAME = 'Hide Native Timeline'
CMD_DESC = "Hide / show Fusion's own Design timeline bar"
PANEL_ID = 'SolidScriptsAddinsPanel'
MAXIMIZE_EVENT = 'MaximizeViewportReapply'
FLAG = '_fusion_hide_native_timeline'

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Hide Native Timeline State.json')


def _load():
    try:
        with open(_STATE_FILE) as f:
            return bool(json.load(f).get('hidden', False))
    except Exception:
        return False


def _save(hidden):
    try:
        with open(_STATE_FILE, 'w') as f:
            json.dump({'hidden': hidden}, f)
    except Exception:
        pass


def _set(hidden):
    setattr(builtins, FLAG, hidden)
    try:
        _app.fireCustomEvent(MAXIMIZE_EVENT, '')
    except Exception:
        pass


class ToggleExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Flip our own flag rather than trusting cmd_def.isChecked (not reliably
        # updated when a checkbox command's execute fires), then force the box to match.
        try:
            hidden = not getattr(builtins, FLAG, False)
            _set(hidden)
            _save(hidden)
            cd = _ui.commandDefinitions.itemById(CMD_ID)
            if cd:
                try:
                    cd.isChecked = hidden
                except Exception:
                    pass
        except Exception:
            if _ui:
                _ui.messageBox(traceback.format_exc())


class ToggleCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = ToggleExecute()
        args.command.execute.add(h)
        _handlers.append(h)


def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        hidden = _load()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addCheckBoxDefinition(CMD_ID, CMD_NAME, CMD_DESC, hidden)
        try:
            cmd_def.isChecked = hidden
        except Exception:
            pass
        on_created = ToggleCreated()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
        _set(hidden)
    except Exception:
        if _ui:
            _ui.messageBox('Hide Native Timeline start failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        _set(False)
        _handlers.clear()
        # leave the command definition so its shortcut binding survives a reload
    except Exception:
        if _ui:
            _ui.messageBox('Hide Native Timeline stop failed:\n{}'.format(traceback.format_exc()))
