#!/usr/bin/env python3
"""
Fusion Params — local web server for the parameter editor.

Shares parameter state between the Fusion add-in (which reads/writes the Fusion
API on the main thread) and the browser UI. The Fusion add-in imports this module
and calls update_params() / pop_commands() from its main-thread sync handler.

Run standalone to serve the UI (params will be empty until Fusion connects).
The 'Fusion Params' add-in imports this and runs make_server() in a daemon thread.
"""

import importlib.util, json, os, queue, threading, time

PORT = 8731
HERE = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = os.path.join(os.path.dirname(HERE), "fusion_ui")
# The shared server plumbing (static + shared assets incl. param_naming.{js,json},
# /settings, CORS) and Fusion keymap reader — see fusion_ui/webwin.py's header.
_spec = importlib.util.spec_from_file_location(
    "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)
keymap = webwin.load("keymap")

GROUPS_FILE = os.path.join(HERE, "groups.json")

_lock = threading.Lock()
_params_snapshot = {"params": [], "ts": 0, "doc_id": ""}
_cmd_queue: queue.Queue = queue.Queue()

# Where the NEXT parameter Fusion reports should land in the browser's group tree.
# The add-in announces a name here just after creating the parameter; the browser
# sees the bumped token on its next poll and files the incoming row under its own
# current selection (see applyPendingInsert in index.js). The browser is the only
# thing that knows what's selected in its window, so the add-in tells it "this one
# is yours" and lets it decide where — rather than shipping selection state to Fusion.
_pending_insert = {"name": "", "token": 0}

# The parameter currently waiting for the user to click a driven dimension in the
# viewport (see _relink_to in the add-in). The add-in owns this: the browser asks for it
# via /relink and then just renders whatever comes back, so a pick that lands, times out,
# or fails clears the window's "picking" state without the browser having to guess.
_relink_armed = {"name": ""}

# Diagnostics, surfaced at /debug. Set by the Fusion add-in's sync handler so we
# can see whether the handler is firing and where it fails, without a debugger.
_debug = {
    "notify_count": 0,   # times the custom-event handler ran
    "last_sync_ts": 0,    # epoch-ms of last successful update_params
    "last_count": 0,      # params seen on last sync
    "design_found": None, # did the handler find an active Design?
    "last_error": "",     # last traceback from the add-in, if any
}


def set_debug(**kw):
    """Called from the Fusion add-in to report sync status (thread-safe)."""
    with _lock:
        _debug.update(kw)


def bump_notify():
    with _lock:
        _debug["notify_count"] += 1


def update_params(params_list, doc_id=""):
    """Called from Fusion main thread. params_list: list of dicts with
    keys: name, expression, unit, comment. doc_id identifies the active
    document so the browser can keep a separate group tree per file."""
    with _lock:
        _params_snapshot["params"] = params_list
        _params_snapshot["doc_id"] = doc_id
        _params_snapshot["ts"] = int(time.time() * 1000)
        _debug["last_sync_ts"] = _params_snapshot["ts"]
        _debug["last_count"] = len(params_list)


def announce_insert(name):
    """Called from the Fusion add-in right after it creates a user parameter, so the
    browser files that row under the group selected in its window instead of at the
    root. Bumping the token is what the browser edge-detects."""
    with _lock:
        _pending_insert["name"] = name
        _pending_insert["token"] += 1


def set_relink_armed(name):
    """Called from the Fusion add-in as it arms/disarms a viewport pick."""
    with _lock:
        _relink_armed["name"] = name or ""


def pop_commands():
    """Called from Fusion main thread. Returns and clears all pending
    {name, expression} commands posted by the browser."""
    return webwin.drain(_cmd_queue)


def _read_groups():
    try:
        with open(GROUPS_FILE) as f:
            return f.read()
    except OSError:
        return json.dumps({"groups": []})


# Command this window is opened by. Used to look up the matching keyboard shortcut
# so the window can close itself when that key is pressed while it's focused.
CMD_ID = "FusionParamsCmd"


class Handler(webwin.Handler):
    HERE = HERE

    def do_GET(self):
        if self.serve_static():
            return
        if self.path == "/params":
            with _lock:
                snap = dict(_params_snapshot)
                snap["pending_insert"] = dict(_pending_insert)
                snap["relink"] = dict(_relink_armed)
                self._send(200, json.dumps(snap))
        elif self.path == "/groups":
            self._send(200, _read_groups())
        # The key Fusion has bound to this window's own command, so the window can
        # close itself when that key is pressed while it's focused.
        elif self.path == "/hotkey":
            self.send_json({"hotkey": keymap.seq_of(keymap.read(), CMD_ID)})
        # Fusion's modifier shortcuts, forwarded while the window is focused. Re-read
        # from disk each request, so a changed Fusion shortcut applies without a
        # restart. Bare keys are this window's own.
        elif self.path == "/fusionkeys":
            self.send_json(keymap.forward_map(keymap.read(), modifiers_only=True))
        elif self.path == "/debug":
            with _lock:
                self._send(200, json.dumps(dict(_debug)))
        else:
            self.not_found()

    def do_POST(self):
        body = self.read_body()

        # /save takes no body — queue a save command for Fusion and return.
        if self.path == "/save":
            _cmd_queue.put({"action": "save"})
            self._send(200, json.dumps({"ok": True}))
            return

        try:
            _peek = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, OSError):
            _peek = {}
        # /command forwards an arbitrary Fusion command to be run by the add-in, so a
        # Fusion modifier shortcut pressed while this window is focused still fires.
        # Arm (name given) or cancel (empty name) a viewport pick for a driven row.
        if self.path == "/relink":
            _cmd_queue.put({"action": "relink",
                            "name": str(_peek.get("name", "")).strip(),
                            "why": str(_peek.get("why", ""))[:40]})
            self._send(200, json.dumps({"ok": True}))
            return
        if self.path == "/command":
            cid = str(_peek.get("cmd_id", "")).strip()
            if cid:
                _cmd_queue.put({"action": "command", "cmd_id": cid})
            self._send(200, json.dumps({"ok": True}))
            return

        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, OSError) as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)}))
            return

        if self.path == "/add":
            name = str(data.get("name", "")).strip()
            expr = str(data.get("expression", "")).strip()
            if not name or not expr:
                self._send(400, json.dumps({"ok": False, "error": "name and expression required"}))
                return
            _cmd_queue.put({
                "action": "add",
                "name": name,
                "expression": expr,
                "unit": str(data.get("unit", "")),
                "comment": str(data.get("comment", "")),
            })
            self._send(200, json.dumps({"ok": True}))

        elif self.path == "/set":
            name = str(data.get("name", "")).strip()
            if not name:
                self._send(400, json.dumps({"ok": False, "error": "name required"}))
                return
            cmd = {"name": name}
            if "newName" in data:
                nn = str(data["newName"]).strip()
                if not nn:
                    self._send(400, json.dumps({"ok": False, "error": "empty new name"}))
                    return
                cmd["newName"] = nn
            if "expression" in data:
                expr = str(data["expression"]).strip()
                if not expr:
                    self._send(400, json.dumps({"ok": False, "error": "empty expression"}))
                    return
                cmd["expression"] = expr
            if "comment" in data:
                cmd["comment"] = str(data["comment"])  # may be empty (clears comment)
            if len(cmd) == 1:
                self._send(400, json.dumps({"ok": False, "error": "nothing to set"}))
                return
            _cmd_queue.put(cmd)
            self._send(200, json.dumps({"ok": True}))

        elif self.path == "/delete":
            name = str(data.get("name", "")).strip()
            if not name:
                self._send(400, json.dumps({"ok": False, "error": "name required"}))
                return
            cmd = {"action": "delete", "name": name}
            if data.get("replacement"):
                cmd["replacement"] = str(data["replacement"]).strip()
            _cmd_queue.put(cmd)
            self._send(200, json.dumps({"ok": True}))

        elif self.path == "/groups":
            try:
                webwin.atomic_write(GROUPS_FILE, json.dumps(data, indent=2))
                self.ok()
            except OSError as e:
                self.fail(500, e)

        elif not self.serve_shared_post(data):
            self.not_found()


def make_server():
    """Return a ready-to-serve TCPServer. Raises OSError if the port is taken."""
    return webwin.make_server(PORT, Handler)


if __name__ == "__main__":
    webwin.main("Fusion Params", make_server, PORT)
