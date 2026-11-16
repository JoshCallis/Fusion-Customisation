#!/usr/bin/env python3
"""
Fusion Shortcuts — local web server for the keyboard-shortcut browser/editor.

Same shape as the Params/Browser companion servers: the Fusion add-in imports
this module, runs make_server() in a daemon thread, and on its main-thread sync
tick pushes the command catalog / recents in and pulls queued actions out.

The keymap has two sources. The add-in pushes Fusion's LIVE table (from the
HotKey.List text command) whenever it's connected, so an edit shows up at once.
When it isn't — the window opened before Fusion, or standalone for styling work —
we fall back to parsing the same table off disk from Fusion's options file:

    ~/Library/Application Support/Autodesk/Neutron Platform/Options/<user>/NGlobalOptions.xml

which is UTF-16 XML holding an HTML-escaped JSON blob in <HotKeyJSONString Value="...">:

    {"hotkeys":[{"file_version":"2"},
                {"hotkey_sequence":"shift+alt+b",
                 "commands":[{"command_id":"ToggleBodyVisibilityCmd",
                              "command_argument":"","isDefault":false}]}, ...]}

isDefault=true  → a binding Fusion ships with
isDefault=false → one the user assigned

Run standalone to serve the UI for styling work (catalog will be empty until the
add-in connects).
"""

import importlib.util, json, os, queue, threading, time

PORT = 8733
HERE = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = os.path.join(os.path.dirname(HERE), "fusion_ui")
# The shared server plumbing (static + shared assets, CORS) and Fusion keymap
# reader — see fusion_ui/webwin.py's header.
_spec = importlib.util.spec_from_file_location(
    "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)
keymap = webwin.load("keymap")

# What the commands are CALLED, when that differs from what Fusion calls them —
# Control Room's filing (remap/fusion_view.json, `commands{id: {label}}`). Read
# here so this window shows the same names, and read by the add-in so the rename
# reaches Fusion's own CommandDefinition (see Shortcuts.py::_apply_labels).
VIEW_FILE = os.path.join(os.path.dirname(HERE), "remap", "fusion_view.json")
# Per-window UI prefs (collapsed groups, last tab). Local to this window.
PREFS_FILE = os.path.join(HERE, "prefs.json")
# Rolling list of recently run commands, written by the add-in so it survives a
# Fusion restart (the in-memory list starts empty every launch otherwise).
RECENTS_FILE = os.path.join(HERE, "recents.json")
# The command-search palette's Show In store: {cmd_id: [env,...]}, written by its
# per-row sheet (fusion_value/index.js). Deliberately its OWN store rather than a
# `when` override in fusion_ui/bindings.json — hiding a command from search must never
# change where its key fires, and it has to work for commands with no binding at all.
# Absent means shown everywhere, so this file stays small and mostly empty.
SEARCH_SHOW_FILE = os.path.join(HERE, "search_show.json")
# The palette's search-only renames: {cmd_id: display name}. Only the palette reads it —
# unlike fusion_view.json labels, it never touches CommandDefinition.name or this window.
SEARCH_NAMES_FILE = os.path.join(HERE, "search_names.json")
# The palette's pinned set: {cmd_id: true}. A pinned command sorts ahead of every
# unpinned one among a query's matches (see searchRank's pinnedRank) — it is still
# ranked and filtered like any other row, just moved to the front of the result.
SEARCH_PINS_FILE = os.path.join(HERE, "search_pins.json")

# The command this window is opened by — used to find its own shortcut so the
# window can close itself when that key is pressed while it's focused.
CMD_ID = "FusionShortcutsCmd"

_lock = threading.Lock()
# Pushed by the add-in's main-thread sync. `commands` is the full catalog:
#   {id, name, tooltip, group, panel, kind}
_catalog = {"commands": [], "groups": [], "ts": 0}
# The LIVE keymap, pushed by the add-in from Fusion's HotKey.List text command.
# Preferred over parsing the options file: it reflects Fusion's in-memory table, so
# a change shows up immediately instead of whenever Fusion next writes the XML.
# Stays None until the add-in connects, and the file parse covers that case.
_live_keys = None
_recents = []          # [{id, ts}] most-recent-first
# Command ids Fusion reported DISABLED the moment the search key was pressed
# (Shortcuts.py::_open_search). Another workspace's commands — Drawing's
# "CircleDiameter", Electronics' "Circle" — are in the catalog but execute() on them
# does nothing, so the palette leaves these out. Empty = unknown = offer everything.
_disabled = []
# The ACTION REGISTRY, pushed by the add-in from fusion_ui/hotkeys.py: every action
# any add-in has declared, the domain it applies in, the key it claims, and the live
# context. This is the half of the keymap Fusion knows nothing about — see
# fusion_ui/hotkeys.py for why the claim list lives here rather than in Fusion's.
_registry = {"actions": [], "bindings": {}, "keys": {}, "axes": {}, "context": {}, "ts": 0}
_cmd_queue: queue.Queue = queue.Queue()
# Result of the last action the add-in ran, surfaced in the UI as a status line.
_result = {"ts": 0, "ok": None, "msg": ""}

_debug = {
    "notify_count": 0,
    "last_sync_ts": 0,
    "catalog_count": 0,
    "last_error": "",
    "write_path": "",     # which write mechanism the add-in settled on
    "probe": "",          # last raw text-command output, for diagnosis
}


# ── Called from the Fusion add-in (main thread) ────────────────────────────────

def set_debug(**kw):
    with _lock:
        _debug.update(kw)


def set_disabled(ids):
    global _disabled
    with _lock:
        _disabled = list(ids)


def bump_notify():
    with _lock:
        _debug["notify_count"] += 1


_labels_cache = {"mtime": None, "labels": {}}


def labels_now():
    """{command id: label} for every rename in Control Room's view file. Re-read
    when the file moves; a missing or broken file is no renames."""
    try:
        m = os.path.getmtime(VIEW_FILE)
    except OSError:
        m = None
    if m != _labels_cache["mtime"]:
        labels = {}
        if m is not None:
            try:
                with open(VIEW_FILE) as f:
                    doc = json.load(f)
                for cid, over in (doc.get("commands") or {}).items():
                    lab = (over or {}).get("label")
                    if isinstance(lab, str) and lab.strip():
                        labels[cid] = lab.strip()
            except (OSError, ValueError):
                pass
        _labels_cache["mtime"] = m
        _labels_cache["labels"] = labels
    return dict(_labels_cache["labels"])


def update_catalog(commands, groups):
    """commands: [{id,name,tooltip,group,panel,kind}], groups: ordered group names."""
    with _lock:
        _catalog["commands"] = commands
        _catalog["groups"] = groups
        _catalog["ts"] = int(time.time() * 1000)
        _debug["last_sync_ts"] = _catalog["ts"]
        _debug["catalog_count"] = len(commands)


def update_recents(recents):
    with _lock:
        _recents[:] = recents


def update_registry(snap):
    """snap: fusion_ui/hotkeys.py::snapshot()."""
    with _lock:
        _registry.update(snap)
        _registry["ts"] = int(time.time() * 1000)


def update_live_keys(entries):
    """entries: the same shape read_hotkeys() returns, parsed from HotKey.List."""
    global _live_keys
    with _lock:
        _live_keys = entries


def set_result(ok, msg):
    with _lock:
        _result.update({"ts": int(time.time() * 1000), "ok": bool(ok), "msg": str(msg)})


def pop_commands():
    """Return and clear every action the window has queued."""
    return webwin.drain(_cmd_queue)


# The add-in's "run a tick now" hook (Shortcuts.py _wake_tick): a /run otherwise
# waits up to the 0.4 s poll — a gesture bound to a command (Control Room's Sends:
# Command) fires through here.
_wake = None


def set_wake(fn):
    global _wake
    _wake = fn


def _wake_addin():
    fn = _wake
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


# ── Files ──────────────────────────────────────────────────────────────────────

_atomic_write = webwin.atomic_write
_read_json = webwin.read_json


# The palette's three stores (show / names / pins) are read-modify-written per
# request, and the server is threaded — without this, two overlapping POSTs each
# read the old file and the second write drops the first's entry.
_store_lock = threading.Lock()


def _update_store(path, mutate):
    with _store_lock:
        store = _read_json(path, {})
        mutate(store)
        _atomic_write(path, json.dumps(store, indent=2))


# ── Fusion's keymap ────────────────────────────────────────────────────────────

read_hotkeys = keymap.read


def hotkey_file():
    return keymap.hotkey_file()


def hotkeys_now():
    """The best available view of the keymap: Fusion's live table when the add-in
    is feeding us, otherwise the options file on disk — with Apply: On restart's
    queued changes replayed on top (keymap.overlay; idempotent, so the add-in having
    already replayed them costs nothing). Here as well as in the add-in so a change
    queued a moment ago is seen now, not on the next push."""
    with _lock:
        live = _live_keys
    return keymap.overlay(live if live is not None else read_hotkeys())


def _has_anchor(seq):
    """Is this key bound in Fusion's own keymap to something we can intercept?

    A key needs exactly ONE binding in Fusion — the anchor — for us to ever hear it
    pressed. The first action assigned to a key creates that anchor through Fusion's
    dialog; every action added afterwards is a bindings.json edit and nothing more."""
    if not seq:
        return True
    with _lock:
        ours = set(_registry.get("bindings", {}))
    for entry in hotkeys_now():
        if entry["seq"] != seq:
            continue
        return any(c["id"] in ours for c in entry["commands"])
    return False


def _read_close_hotkey():
    """The sequence Fusion has bound to this window's own command, or ''."""
    return keymap.seq_of(hotkeys_now(), CMD_ID)


def _read_all_hotkeys():
    """{sequence: command_id} for every binding that uses a modifier. The window
    forwards these to Fusion while it's focused, so Fusion's modifier shortcuts
    keep working inside the window. Bare keys are the window's own."""
    return keymap.forward_map(hotkeys_now(), modifiers_only=True)


# ── HTTP ───────────────────────────────────────────────────────────────────────

class Handler(webwin.Handler):
    HERE = HERE

    def do_GET(self):
        path = self.route
        if self.serve_static():
            return

        # Everything the UI needs in one round-trip.
        if path == "/data":
            with _lock:
                cat = dict(_catalog)
                rec = list(_recents)
                res = dict(_result)
                # Heartbeat of the add-in's main-thread sync. It stops advancing
                # whenever Fusion's main thread is blocked — which is exactly what a
                # modal Fusion dialog does — so the window can use it to tell when
                # it must not raise itself over one.
                beat = _debug["notify_count"]
                reg = dict(_registry)
                disabled = list(_disabled)
            # The catalog carries Fusion's names; the view's labels win in this
            # window, the same as they do in Control Room. Applied per answer so a
            # rename shows on the next poll, not the next catalog build; Fusion's
            # own name is kept as `native`.
            labels = labels_now()
            commands = cat["commands"]
            if labels:
                commands = [dict(c, native=c.get("name"), name=labels[c["id"]])
                            if c.get("id") in labels else c for c in commands]
            self._send(200, json.dumps({
                "registry": reg,
                "commands": commands,
                "groups":   cat["groups"],
                "ts":       cat["ts"],
                "hotkeys":  hotkeys_now(),
                "recents":  rec,
                "result":   res,
                "beat":     beat,
                "labels":   labels_now(),
                "search_show": _read_json(SEARCH_SHOW_FILE, {}),
                "search_names": _read_json(SEARCH_NAMES_FILE, {}),
                "search_pins": _read_json(SEARCH_PINS_FILE, {}),
                "disabled": disabled,
            }))

        elif path == "/actions":
            with _lock:
                self._send(200, json.dumps(dict(_registry)))

        elif path == "/hotkeys":
            self._send(200, json.dumps(hotkeys_now()))
        elif path == "/hotkey":
            self._send(200, json.dumps({"hotkey": _read_close_hotkey()}))
        elif path == "/fusionkeys":
            self._send(200, json.dumps(_read_all_hotkeys()))
        elif path == "/prefs":
            self._send(200, json.dumps(_read_json(PREFS_FILE, {})))
        elif path == "/debug":
            with _lock:
                d = dict(_debug)
            d["hotkey_file"] = hotkey_file()
            d["hotkey_count"] = len(hotkeys_now())
            d["live_keys"] = _live_keys is not None
            self._send(200, json.dumps(d, indent=2))
        else:
            self.not_found()

    def do_POST(self):
        data = self.read_json_body()
        if data is None:
            return
        if not isinstance(data, dict):
            data = {}

        path = self.route

        # Assign / change / clear a shortcut. All three are the same action: hand
        # the command to Fusion's own "Change Keyboard Shortcut" prompt, which
        # applies the binding live and persists it. The window never edits the
        # keymap itself.
        if path in ("/assign", "/dialog", "/unassign"):
            cid = str(data.get("cmd_id", "")).strip()
            if not cid:
                self._send(400, json.dumps({"ok": False, "error": "cmd_id required"}))
                return
            _cmd_queue.put({"action": "assign", "cmd_id": cid})
            self._send(200, json.dumps({"ok": True}))

        # Point one action at one key, or clear it. This writes ONLY that action's
        # row in fusion_ui/bindings.json — every other claimant of the key is left
        # exactly as it was, which is the property Fusion's own keymap cannot give
        # us. The reply says whether Fusion still needs an anchor for the key (see
        # hotkeys.py): if it does, the window follows up with /assign.
        if path in ("/bind", "/unbind"):
            cid = str(data.get("cmd_id", "")).strip()
            seq = "" if path == "/unbind" else str(data.get("seq", "")).strip()
            if not cid:
                self._send(400, json.dumps({"ok": False, "error": "cmd_id required"}))
                return
            msg = {"action": "bind", "cmd_id": cid, "seq": seq}
            # Tri-state, and it travels as a plain in-process dict (never JSON in
            # between), so the key's presence survives to Shortcuts.py untouched:
            # omitted -> leave the command's override as it is; {} -> clear it;
            # {...} -> set it. See fusion_ui/hotkeys.py::set_binding.
            if "when" in data:
                w = data["when"]
                msg["when"] = w if isinstance(w, dict) else {}
            if "typing" in data:
                msg["typing"] = bool(data["typing"])
            _cmd_queue.put(msg)
            # `anchored` is kept for this window's own bindAction() flow, which
            # only ever asks about ITS OWN declared actions with no domain split —
            # Control Room's Fusion Shortcuts page computes its own plan instead
            # (remap/fusion.py::anchor_plan, from /hotkeys + its rows), because
            # that one has to reason about `when`, which this boolean cannot.
            self._send(200, json.dumps({"ok": True, "anchored": _has_anchor(seq)}))
            return

        # A synthetic anchor for two-or-more declared actions sharing a chord —
        # see fusion_ui/hotkeys.py's ROUTE_PREFIX and remap/fusion.py::apply_plan,
        # which calls this before anchoring the route itself.
        if path == "/route":
            seq = str(data.get("seq", "")).strip()
            if not seq:
                self._send(400, json.dumps({"ok": False, "error": "seq required"}))
                return
            _cmd_queue.put({"action": "route", "seq": seq})
            self._send(200, json.dumps({"ok": True}))
            return

        # A chord captured by a companion window's own keydown hook rather than
        # Fusion's keymap (Fusion never sees it) — currently fusion_value's Extrude
        # panel, for rows with typing enabled. See hotkeys.py::press/typing_keys.
        if path == "/press":
            seq = str(data.get("seq", "")).strip()
            if not seq:
                self._send(400, json.dumps({"ok": False, "error": "seq required"}))
                return
            # "any": the panel has no text field (the mirror panel), so the chord runs
            # whatever wins it, not only an Active While Typing row — hotkeys.press.
            _cmd_queue.put({"action": "press", "seq": seq, "any": bool(data.get("any"))})
            self._send(200, json.dumps({"ok": True}))
            return

        # Run a Fusion command by id (row double-click, and forwarding Fusion's
        # own modifier shortcuts pressed while this window has focus).
        elif path in ("/run", "/command"):
            cid = str(data.get("cmd_id", "")).strip()
            if cid:
                _cmd_queue.put({"action": "command", "cmd_id": cid})
                _wake_addin()
            self._send(200, json.dumps({"ok": True}))

        # Fall back to Fusion's own "Change Keyboard Shortcut" dialog.
        elif path == "/dialog":
            cid = str(data.get("cmd_id", "")).strip()
            if cid:
                _cmd_queue.put({"action": "dialog", "cmd_id": cid})
            self._send(200, json.dumps({"ok": True}))

        # Diagnostic: run an arbitrary Fusion text command and stash the output at
        # /debug. Localhost-only, and the only way to inspect the undocumented
        # HotKey.* text commands from outside Fusion.
        elif path == "/probe":
            txt = str(data.get("text", "")).strip()
            if txt:
                _cmd_queue.put({"action": "probe", "text": txt})
            self._send(200, json.dumps({"ok": True}))

        # Rebuild the command catalog (the walk over every workspace/tab/panel is
        # too costly to run on every sync tick, so the window asks when it opens).
        elif path == "/rebuild":
            _cmd_queue.put({"action": "rebuild"})
            self._send(200, json.dumps({"ok": True}))

        elif path == "/prefs":
            try:
                _atomic_write(PREFS_FILE, json.dumps(data, indent=2))
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))

        # The command-search palette's Show In sheet. Served inline rather than queued,
        # like /prefs above and fusion_value's own /search/close — it touches no Fusion
        # API. Always writes the full list for the id rather than deleting the entry
        # even when all three are ticked: cheap, and it keeps "explicitly everywhere"
        # distinguishable from "never touched". An empty list is the None box — hidden
        # in every environment, deliberately allowed (unlike Control Room's Applies In,
        # which refuses to let a binding apply nowhere).
        elif path == "/search_show":
            cid = str(data.get("cmd_id", "")).strip()
            if not cid:
                self._send(400, json.dumps({"ok": False, "error": "cmd_id required"}))
                return
            envs = data.get("env")
            if not isinstance(envs, list):
                envs = []
            envs = [e for e in envs if e in ("sketch", "viewport", "extrude")]
            try:
                _update_store(SEARCH_SHOW_FILE, lambda st: st.__setitem__(cid, envs))
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))

        # The palette's rename, same shape as /search_show. A blank name drops the entry.
        elif path == "/search_name":
            cid = str(data.get("cmd_id", "")).strip()
            if not cid:
                self._send(400, json.dumps({"ok": False, "error": "cmd_id required"}))
                return
            name = str(data.get("name") or "").strip()
            try:
                _update_store(SEARCH_NAMES_FILE, lambda st: st.__setitem__(cid, name)
                              if name else st.pop(cid, None))
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))

        # The palette's Pin checkbox, same shape as /search_show and /search_name.
        # Absent = unpinned; only a pinned id is ever stored, so the file stays small.
        elif path == "/search_pin":
            cid = str(data.get("cmd_id", "")).strip()
            if not cid:
                self._send(400, json.dumps({"ok": False, "error": "cmd_id required"}))
                return
            pinned = bool(data.get("pinned"))
            try:
                _update_store(SEARCH_PINS_FILE, lambda st: st.__setitem__(cid, True)
                              if pinned else st.pop(cid, None))
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))

        else:
            self._send(404, b"not found", "text/plain")


def make_server():
    """Ready-to-serve TCPServer. Raises OSError if the port is taken."""
    return webwin.make_server(PORT, Handler)


if __name__ == "__main__":
    webwin.main("Fusion Shortcuts", make_server, PORT)
