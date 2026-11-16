"""
Scroll Curve Editor — Fusion launcher add-in.

This add-in's only job is a toolbar button that serves the standalone web editor
and opens it in the browser. The editor itself (server.py + index.html) lives in
~/Code/Scripts/scroll_curve_editor/ and writes the overlay-curve files
(/tmp/fusion_vel_adjust, /tmp/fusion_prox_adjust) that FusionScrollFilter reads live.

The HTTP server is run in a daemon THREAD inside Fusion's own Python — it only
reads/writes /tmp files and never touches the Fusion API, so it's thread-safe.
(We do NOT subprocess.Popen sys.executable: inside Fusion that is the Fusion app
binary, not Python, so it would launch a second Fusion.)

Follows the reload/shortcut-safe boilerplate (find-or-create cmd def, handlers.clear()
in stop() without deleting the cmd def, stale-handler guard in notify()).
"""

import adsk.core, traceback, subprocess, threading, importlib.util, os

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

CMD_ID = 'ScrollCurveEditorCmd'
CMD_NAME = 'Scroll Curve Editor'
CMD_DESC = 'Open the graphical scroll-curve editor (velocity + proximity) in your browser'
PANEL_ID = 'SolidScriptsAddinsPanel'

EDITOR_DIR = '/Users/josh/Code/Scripts/scroll_curve_editor'
SERVER_PY = os.path.join(EDITOR_DIR, 'server.py')

_httpd = None          # the running server, if this add-in started it
_server_mod = None     # the imported server module (for PORT / make_server)


def _load_server_module():
    global _server_mod
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('scroll_editor_server', SERVER_PY)
        _server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_server_mod)
    return _server_mod


def _launch_editor():
    """Start the server in a background thread (if not already up), then open the page."""
    global _httpd
    mod = _load_server_module()
    url = 'http://localhost:%d' % mod.PORT
    if _httpd is None:
        try:
            _httpd = mod.make_server()
            threading.Thread(target=_httpd.serve_forever, daemon=True).start()
        except OSError:
            # Port already in use — an instance is already serving. Just open it.
            _httpd = None
    subprocess.Popen(['open', url])


def _shutdown_server():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None


def run(context):
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        h = CommandCreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Scroll Curve Editor failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        handlers.clear()
        _shutdown_server()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        # Do NOT delete the command definition — that would drop the shortcut binding.
    except Exception:
        pass


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:        # stale handler from a previous load
                return
            on_exec = CommandExecuteHandler()
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
        except Exception:
            if ui:
                ui.messageBox('Error in commandCreated:\n{}'.format(traceback.format_exc()))


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _launch_editor()
        except Exception:
            if ui:
                ui.messageBox('Error launching editor:\n{}'.format(traceback.format_exc()))
