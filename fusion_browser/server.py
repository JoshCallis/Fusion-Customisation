#!/usr/bin/env python3
"""
Fusion Browser — local web server for the custom component browser.

Mirrors the Fusion Params architecture: the Fusion add-in imports this module and,
from its main-thread sync handler, calls update_state() with a hierarchy snapshot
and pop_commands() to drain UI actions. The browser UI (a WKWebView app) polls
/state and POSTs actions to /cmd. The HTTP server thread only ever touches the
in-memory snapshot + command queue under a lock — never the Fusion API.

Run standalone to serve the UI (it'll show "waiting" until Fusion connects).
"""

import builtins, hashlib, importlib.util, json, os, queue, subprocess, threading, time

PORT = 8732
HERE = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = os.path.join(os.path.dirname(HERE), "fusion_ui")
# The shared server plumbing (static + shared assets, /settings, CORS) and Fusion
# keymap reader — see fusion_ui/webwin.py's header.
_spec = importlib.util.spec_from_file_location(
    "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)
keymap = webwin.load("keymap")

# Window-only "virtual folder" overlay, per document. Folders group a component's
# sketches and bodies in THIS browser only — the Fusion hierarchy is untouched — so
# they live here rather than in the model. Mirrors Params' groups.json.
FOLDERS_FILE = os.path.join(HERE, "folders.json")
# Shared action manifest. One entry names an action that must work whichever surface
# has focus; the id is a real Fusion command id, so it is bindable in Fusion's own
# keymap and there is no second keymap to keep in step. See the file's own comment.
ACTIONS_FILE = webwin.shared("actions.json")


def read_actions():
    """The manifest, as a list of dicts. Re-read per call so adding an action needs no
    restart of anything but the add-in that creates its CommandDefinition."""
    try:
        with open(ACTIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    acts = data.get("actions") if isinstance(data, dict) else None
    return [a for a in (acts or []) if isinstance(a, dict) and a.get("id")]


def browser_action_ids():
    """Ids whose behaviour lives in the window's JS — the ones Fusion forwards here."""
    return {a["id"] for a in read_actions() if a.get("home") == "browser"}


def manifest_command_ids():
    """Every id the manifest names, whatever its home.

    Used to disambiguate a shared key sequence: if one of these is on the entry, it is
    by definition the id this window means by that key.
    """
    return {a["id"] for a in read_actions()}


_atomic_write = webwin.atomic_write
_folders_lock = threading.Lock()


def _read_folders():
    try:
        with open(FOLDERS_FILE) as f:
            return f.read()
    except OSError:
        return json.dumps({"byDoc": {}})


def _write_folders(doc_id, data):
    """Replace one document's folder overlay, leaving every other document's alone."""
    with _folders_lock:
        try:
            with open(FOLDERS_FILE) as f:
                raw = f.read()
            allf = json.loads(raw)
        except (OSError, ValueError):
            raw, allf = "", {}
        if not isinstance(allf, dict):
            allf = {}
        by_doc = allf.get("byDoc")
        if not isinstance(by_doc, dict):
            by_doc = allf["byDoc"] = {}
        # One step back, kept before every write that actually changes something. The
        # folders file is the ONLY copy of the groups, their membership and where each
        # parameter is filed — hundreds of decisions that cannot be reconstructed from
        # the model — and a window bug once wrote the whole overlay away as `{}` on a
        # page load. Restoring it took a snapshot that happened to be lying around.
        old = by_doc.get(doc_id)
        if raw and old != data:
            try:
                _atomic_write(FOLDERS_FILE + ".bak", raw)
            except OSError:
                pass
        by_doc[doc_id] = data
        _atomic_write(FOLDERS_FILE, json.dumps(allf, indent=2))


def carry_leaf_folders(doc_id, moves):
    """Move one document's leaf filings from old keys to new ones, all at once
    ([(old, new)] — one move's old key can be another's new). Called by the add-in
    when it stamps body ids (Browser.py _stamp_bodies): the window carries its own
    copy on the same-token-new-key signal, but only if it is polling at that moment —
    this is what keeps the filings when it isn't. A live window that saves its older
    copy over this carries it again on its next tree, so the two agree either way."""
    moves = [(o, n) for o, n in (moves or ()) if o and n and o != n]
    if not moves or not doc_id:
        return
    try:
        allf = json.loads(_read_folders())
        doc = dict((allf.get("byDoc") or {}).get(doc_id) or {})
    except ValueError:
        return
    leaves = dict(doc.get("leaves") or {})
    was = [(n, leaves.get(o)) for o, n in moves]
    if not any(f for _n, f in was):
        return
    for o, _n in moves:
        leaves.pop(o, None)
    for n, f in was:
        if f:
            leaves[n] = f
        else:
            leaves.pop(n, None)
    doc["leaves"] = leaves
    _write_folders(doc_id, doc)


_lock = threading.Lock()
# selTokens = list of tree-row tokens for everything currently selected in the
# viewport (mirrored into the browser as a real selection). Replaces the old
# single selToken / blue-accent-bar mirror.
# rootName = the root component's own name. It has no row in the tree (the document
# root is drawn as the top level, not as a component), but a structured parameter name
# can still be OWNED by it — its first level is the owner component's identifier, and
# the root is one of the components the value dialog's owner picker offers. Without the
# name here the window cannot tell a root-owned parameter from one whose component has
# been deleted, and would file it under a group named after the document instead of at
# the top level where it belongs.
# rolledBack = the timeline marker is short of its end. A component missing from the
# tree then may just not exist YET, so the window hides what is tied to it (its
# folders, parameter groups, name groups) instead of drawing them at the top level.
_state = {"ts": 0, "doc_id": "", "activeToken": "", "selTokens": [], "rootName": "",
          "rolledBack": False,
          "toggles": {}, "tree": [], "editingSketch": False, "editSketchToken": "",
          "uiActions": [], "uiSeq": 0, "navSeq": 0}
# The window outlives this server, so uiSeq starts from the clock — see
# webwin.seq_base. navSeq stays at 0: a jump in it IS the signal (noteNavJump arms a
# forced tidy), so a clock seed would fire one on every restart; the page re-adopts a
# navSeq that went backwards instead.
_state["uiSeq"] = webwin.seq_base()
_cmd_queue: queue.Queue = queue.Queue()
_appearances = {"rev": 0, "lists": {"design": [], "library": []}}

_debug = {"notify_count": 0, "last_sync_ts": 0, "last_nodes": 0,
          "design_found": None, "last_error": ""}


def set_debug(**kw):
    with _lock:
        _debug.update(kw)


def set_appearances(lists):
    """The add-in's answer to an `appearances` command (and after every paint): the
    palette's swatches, {design: [...], library: [...]}. Served at /appearances with a
    rev the page watches so a paint refreshes the "in this design" row."""
    with _lock:
        _appearances["lists"] = lists
        _appearances["rev"] += 1


# Texture swatches: the Material Library's own colour maps, shrunk once with sips (a
# full wood map is megabytes; the swatch is 26 px). Only files under the library root.
MATLIB_ROOT = os.path.expanduser("~/Library/Application Support/Autodesk/Common/Material Library")
APPTEX_CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fusion_browser_apptex")


def _apptex(path):
    real = os.path.realpath(path or "")
    if not real.startswith(os.path.realpath(MATLIB_ROOT) + os.sep) or not os.path.isfile(real):
        return None
    os.makedirs(APPTEX_CACHE, exist_ok=True)
    thumb = os.path.join(APPTEX_CACHE, hashlib.md5(real.encode()).hexdigest()[:16] + ".jpg")
    if not os.path.isfile(thumb):
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "96", real, "--out", thumb],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    try:
        with open(thumb, "rb") as f:
            return f.read()
    except OSError:
        return None


def set_sketch_deps(seq, names, kinds=None, keys=None):
    """The add-in's answer to a `sketchdeps` command: what stands on the sketches a
    right-click menu is open on, each one's kind ('sketch' / 'feature') and a sketch's
    row key — both parallel to `names`. `seq` is the window's, so it can tell a stale
    answer."""
    with _lock:
        _state["sketchDeps"] = {"seq": seq, "names": list(names or []),
                                "kinds": list(kinds or []), "keys": list(keys or [])}


def set_group_uses(uses):
    """{folder id: [combine names]} — the groups a live Combine run follows (AddIns/
    Combine's LINK). The window warns before exploding one."""
    with _lock:
        _state["groupUses"] = dict(uses or {})


_wake = None


def set_wake(fn):
    """The add-in's "run a tick now" hook. A queued /cmd otherwise waits for the next
    70 ms poll — 35 ms on average, on every Enter/D/click the window sends."""
    global _wake
    _wake = fn


def _wake_addin():
    fn = _wake
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def bump_notify():
    with _lock:
        _debug["notify_count"] += 1


_freeze_until = 0.0


def set_freeze(seconds=0.0):
    """Hold the currently published snapshot for up to `seconds` (0 clears it).

    Flush Undo closes and reopens the document, and while it is closed the sync
    tick has no design to walk, so it would publish an empty tree — the window
    would blank and repopulate on every flush. Freezing keeps the last good
    snapshot on screen for the whole round trip, so nothing jumps.

    Deliberately time-bounded: if a flush dies between freeze and clear, the
    window starts updating again by itself instead of being stuck forever.
    """
    global _freeze_until
    with _lock:
        _freeze_until = (time.time() + seconds) if seconds and seconds > 0 else 0.0


def is_frozen():
    with _lock:
        return bool(_freeze_until and time.time() < _freeze_until)


def update_state(tree, active_token="", toggles=None, doc_id="", sel_tokens=None,
                 editing_sketch=False, edit_sketch_token="", click_ts=0,
                 root_name="", rolled_back=False, sel_quiet=False,
                 sel_feats=None, sel_pick=None, comps=None):
    """Called from the Fusion main thread with the full hierarchy snapshot.
    `comps` = [[component id, name]] for every component, root aside — the Hide
    Edges flyout's list (the tree leaves disregarded ones out)."""
    with _lock:
        if _freeze_until and time.time() < _freeze_until:
            return
        _state["tree"] = tree
        _state["comps"] = list(comps or [])
        _state["rootName"] = root_name or ""
        _state["rolledBack"] = bool(rolled_back)
        _state["activeToken"] = active_token
        _state["selTokens"] = list(sel_tokens or [])
        _state["selQuiet"] = bool(sel_quiet)     # mirror it, don't open the tree to it
        _state["selFeats"] = list(sel_feats or [])   # quiet: pick these feature rows
        _state["selPick"] = dict(sel_pick or {})     # body / part-of-body pick: open it
        _state["toggles"] = toggles or {}
        _state["doc_id"] = doc_id
        _state["editingSketch"] = bool(editing_sketch)
        _state["editSketchToken"] = edit_sketch_token or ""
        # clickTs is patch_click's (click_ts is still accepted from older add-ins):
        # this only runs when the tree changes, and a click on empty space changes
        # nothing, so a stamp carried here arrived late or never.
        if "clickCanvas" not in _state:
            _state["clickTs"] = int(click_ts or 0)
        _state["ts"] = int(time.time() * 1000)
        _debug["last_sync_ts"] = _state["ts"]
        _debug["last_nodes"] = len(tree)


def patch_click(click_ts, over_canvas):
    """Stamp of the last left-click over Fusion, and whether it landed on the 3D canvas.
    Called every sync tick — not only when the tree changes — so the window hears about
    a click on empty space even though it changes nothing else. The window uses it to
    dismiss its context menu, and to drop a selection the viewport can't hold
    (parameters, groups) when you click empty canvas."""
    with _lock:
        if _freeze_until and time.time() < _freeze_until:
            return
        _state["clickTs"] = int(click_ts or 0)
        _state["clickCanvas"] = bool(over_canvas)


def patch_peek(token):
    """Suspend mode's peek (fvs § Suspend mode: peek) as the row token the window
    roots on — "__root__" for the root, None for no peek. Every sync tick, like
    patch_click: a peek from a viewport key changes no tree."""
    with _lock:
        if _freeze_until and time.time() < _freeze_until:
            return
        _state["peekToken"] = token


def patch_extrude(on, keys=None):
    """Whether our Extrude is open and which bodies it works on (body row keys).
    Every sync tick, like patch_click — none of it changes the tree. While it is on the
    window turns a body or group click into `extrudebodies` instead of selecting."""
    with _lock:
        if _freeze_until and time.time() < _freeze_until:
            return
        _state["extrude"] = {"on": bool(on), "keys": list(keys or [])}


def patch_active(active_token="", editing_sketch=False, edit_sketch_token=""):
    """Optimistically patch just the active-component fields of the current snapshot
    (without a full tree rebuild), so a viewport-driven jump the add-in just triggered
    (e.g. double-click → Go to Top) shows in the browser on the very next poll instead of
    waiting for the next full sync tick."""
    with _lock:
        if _freeze_until and time.time() < _freeze_until:
            return
        _state["activeToken"] = active_token
        _state["editingSketch"] = bool(editing_sketch)
        _state["editSketchToken"] = edit_sketch_token or ""
        _state["ts"] = int(time.time() * 1000)


_ui_seq = _state["uiSeq"]
UI_ACTION_KEEP = 20

# When the window last asked for /state. The page polls every 80 ms while it is open, so
# this is a cheap, accurate liveness signal — no pgrep, nothing to keep in sync.
_last_poll = 0.0


def window_alive(max_age=2.0):
    """True if the browser window has polled recently enough to act on a forwarded key.

    Forwarding is only safe while something is there to receive it. An action whose home
    is the window would otherwise be vetoed in Fusion and then dropped on the floor with
    the dock closed — which for an id that also has real Fusion behaviour (Deselect All)
    would mean its toolbar button quietly stopped working. The veto consults this and
    lets the command run natively when the window is gone.
    """
    return (time.time() - _last_poll) < max_age


# What the WINDOW is currently holding, pushed here whenever it changes. The add-in
# reads this on Fusion's main thread from commandStarting, where it has microseconds to
# decide whether ⌘X / ⌘V mean something to the browser or should be left to Fusion's
# own Cut and Paste — so it has to be an in-memory flag, not a question asked over HTTP.
_ui_flags = {
    "canCut": False,   # the browser's selection includes a parameter ⌘X could take
    "hasCut": False,   # a cut is pending, waiting for a ⌘V to place it
    "showOrigins": False,   # the Origins bar switch — gate on reading 7 bulbs/component
    # The Construction Planes switch. None until the window says — the add-in only
    # writes Object Visibility on a real value (Browser.py _sync_construction_vis).
    "showConstruction": None,
}


def ui_flags():
    with _lock:
        return dict(_ui_flags)


# Who owns ⌘V — the ledger the Sketch Clipboard add-in shares with this window (see
# "Who owns ⌘V" in its .py). A paste places whichever was taken LAST: a sketch copied
# there, or a parameter cut here. This side writes it when a cut starts ("browser",
# pending) and when it ends — placed or Esc'd — (pending false, only while it is still
# ours: a sketch copy that cleared the cut has already written "sketch").
CLIP_OWNER_FILE = os.path.expanduser(
    "~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/Clipboard Owner.json")


def _note_cut(pending):
    try:
        with open(CLIP_OWNER_FILE, encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    if not pending and (not isinstance(cur, dict) or cur.get("who") != "browser"):
        return
    try:
        tmp = CLIP_OWNER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"who": "browser", "ts": time.time(), "pending": bool(pending)}, f)
        os.replace(tmp, CLIP_OWNER_FILE)
    except OSError:
        pass


def push_ui_action(action_id, arg=None):
    """Queue an action for the WINDOW to run. The exact reverse of _cmd_queue.

    A hotkey pressed while the viewport has focus reaches Fusion first. If the action's
    home is this window, the add-in vetoes the Fusion command and drops the id here; the
    window picks it up on its next /state poll and runs the same JS the key would have
    run had the window been focused. One binding, either surface.

    Rides /state rather than its own endpoint because /state is already polled every
    80 ms — a second request per action would double the traffic to save nothing.

    Deliberately NOT gated by the freeze: freezing holds the tree snapshot still while a
    document reopens, and it must never swallow a keystroke. Each entry carries a
    monotonic seq so the window runs it exactly once, and the list is trimmed so a
    window that has been closed for an hour cannot bank keypresses; the page also adopts
    the current seq on its first poll rather than replaying whatever is still in here.
    """
    global _ui_seq
    with _lock:
        _ui_seq += 1
        kept = (_state.get("uiActions") or [])[-(UI_ACTION_KEEP - 1):]
        _state["uiActions"] = kept + [{"seq": _ui_seq, "id": action_id, "arg": arg}]
        _state["uiSeq"] = _ui_seq


_nav_seq = 0


def note_nav_jump():
    """Record that a DELIBERATE navigation command just started (go to top / bottom).

    The window follows the viewport by watching the active component change, and that
    is enough for almost everything. It is not enough on the way out of a sketch: the
    window deliberately protects the edited sketch's owning component from the spine
    tidy for a few seconds, because Fusion activates the parent for a beat before
    landing back in the owner and collapsing/re-expanding across that would flicker.
    A jump to the top level pressed FROM INSIDE a sketch exits the sketch and moves
    levels in one go, so it landed inside that protection and the tree stayed open on
    the component the user had just left — the same jump made outside a sketch tidied
    normally.

    The window cannot tell the two apart from the state alone (the transient parent
    activation of a plain sketch exit IS the top level, when the sketch's owner sits
    directly under it), so the add-in says so explicitly. A monotonic counter, read on
    the /state poll the window already makes; the window arms a short "tidy regardless"
    window when it moves.

    Deliberately NOT gated by the freeze, for the same reason push_ui_action isn't: this
    is an event, not a snapshot, and swallowing it loses the tidy for good.
    """
    global _nav_seq
    with _lock:
        _nav_seq += 1
        _state["navSeq"] = _nav_seq


def pop_commands():
    return webwin.drain(_cmd_queue)


# bin/fusion_busy's off switch (the spinner by the pointer while Fusion is busy).
BUSY_OFF = os.path.join(os.path.dirname(HERE), "config", "fusion_busy_off")


def dock_mode():
    """Which panel the dock shows — "browser" or "timeline" (the vertical timeline,
    fusion_timeline/). The Timeline add-in owns it on builtins._fusion_dock; the page
    tells the dock shell when it changes. No add-in (standalone): the Browser."""
    d = getattr(builtins, "_fusion_dock", None)
    return d["mode"] if isinstance(d, dict) and d.get("mode") in ("browser", "timeline") else "browser"


# ── Fusion keyboard shortcuts (read from NGlobalOptions.xml) ──────────────────
# Lets the window forward the body/sketch/origin/isolate toggle keys to Fusion
# (so the same key works in both places) and close itself on its own open-key.
# command_id -> short name the UI uses.
HOTKEY_CMDS = {
    "FusionBrowserCmd":               "close",
    "ToggleBodyVisibilityCmd":        "bodies",
    "ToggleSketchVisibilityCmd":      "sketches",
    "BrowserToggleOriginsCmd":        "origins",
    "ToggleIsolateActiveComponentCmd": "isolate",
}


def _read_hotkeys():
    out = {}
    for e in keymap.read():
        for c in e["commands"]:
            if c["id"] in HOTKEY_CMDS:
                out[HOTKEY_CMDS[c["id"]]] = e["seq"]
    return out


def _read_all_hotkeys():
    """The FULL Fusion keymap as {hotkey_sequence: command_id} — the window forwards any
    of these to Fusion when it's focused, so all of Fusion's shortcuts keep working
    inside the window. Re-read from disk each request, so adding/changing a Fusion
    shortcut takes effect without restarting anything. Our own manifest ids win a
    shared sequence (keymap.design_command's `prefer`).

    Bare single keys are INCLUDED. They used to be dropped here on the grounds that they
    were the window's own nav keys, which is exactly what forced every window action to
    carry a second, hand-maintained binding. The window now decides per id instead: one
    of our own action ids it runs locally, anything else it forwards. Keys the window
    must keep for itself (Return, Backspace, Escape) are held back on the page, where
    the reason — "this key already means something here" — actually lives."""
    out = keymap.forward_map(keymap.read(), prefer=manifest_command_ids())
    _add_remap_translations(out)
    return out


# remapd rewrites some keys before Fusion ever sees them — S becomes ⌥⇧⌘S, the chord
# Command Search is bound to — but only in situations that need FUSION frontmost
# (`fusion-idle`; `fusion-space`, whose held Space is a Karabiner rule keyed to Fusion
# frontmost too). With a Fusion window focused (the dock takes key on hover) the key
# arrives untranslated, so it wouldn't match Fusion's keymap — or matched the BARE key:
# Space+T over the timeline ran T (Trim, which starts a sketch) instead of the timeline
# toggle. Folding the translation in makes the key mean what it means in the viewport.
# A Space-held row is spelled "space+<key>" — the chord Keys.seqOf (fusion_ui/keys.js)
# builds while the page sees Space held.
REMAP_BINDINGS = os.path.join(os.path.dirname(HERE), "remap", "bindings.json")
_REMAP_SITUATIONS = {"fusion-idle": "", "fusion-space": "space+"}
_MOD_ALIASES = {"command": "cmd", "meta": "cmd", "option": "alt", "control": "ctrl"}


def _canon_chord(seq):
    parts = [_MOD_ALIASES.get(p, p) for p in (seq or "").lower().split("+") if p]
    return frozenset(parts)


def _add_remap_translations(out):
    try:
        with open(REMAP_BINDINGS) as f:
            bindings = json.load(f).get("bindings", [])
    except Exception:
        return
    by_chord = {_canon_chord(seq): cid for seq, cid in out.items()}
    for b in bindings:
        if not isinstance(b, dict) or b.get("enabled") is False:
            continue
        sits = b.get("situation")
        sits = sits if isinstance(sits, list) else [sits]
        key, to = (b.get("key") or "").lower(), b.get("to") or ""
        cid = by_chord.get(_canon_chord(to))
        if not key or not cid:
            continue
        for sit in sits:
            prefix = _REMAP_SITUATIONS.get(sit) if isinstance(sit, str) else None
            if prefix is not None and _canon_chord(prefix + key) not in by_chord:
                out[prefix + key] = cid


class Handler(webwin.Handler):
    HERE = HERE

    def do_GET(self):
        if self.serve_static():
            return
        if self.path == "/state":
            global _last_poll
            _last_poll = time.time()
            with _lock:
                s = dict(_state)
            s["dockMode"] = dock_mode()
            s["busySpinner"] = not os.path.exists(BUSY_OFF)
            self._send(200, json.dumps(s))
        elif self.path.split("?", 1)[0] == "/appearances":
            # ?since=<rev> answers just the rev when nothing changed — the page polls
            # this while its palette is up, and the library alone is ~180 entries.
            from urllib.parse import parse_qs, urlsplit
            since = (parse_qs(urlsplit(self.path).query).get("since") or [""])[0]
            with _lock:
                if since == str(_appearances["rev"]):
                    self._send(200, json.dumps({"rev": _appearances["rev"]}))
                else:
                    self._send(200, json.dumps(_appearances))
        elif self.path.startswith("/apptex?"):
            from urllib.parse import parse_qs, urlsplit
            p = (parse_qs(urlsplit(self.path).query).get("p") or [""])[0]
            data = _apptex(p)
            if data is None:
                self.not_found()
            else:
                self._send(200, data, "image/jpeg")
        elif self.path == "/hotkeys":
            self._send(200, json.dumps(_read_hotkeys()))
        elif self.path == "/fusionkeys":
            self._send(200, json.dumps(_read_all_hotkeys()))
        elif self.path == "/folders":
            self._send(200, _read_folders())
        elif self.path == "/paramshow":
            # The open document's shown/hidden row parts ({param name: {name?, type?,
            # dir?}}) — FusionValue's stepper reads them to draw its crossed-out chips.
            # Read-only: the page is the one writer (BrowserParamShowCmd).
            try:
                allf = json.loads(_read_folders())
                with _lock:
                    doc = _state.get("doc_id") or ""
                show = ((allf.get("byDoc") or {}).get(doc) or {}).get("show") or {}
            except (ValueError, AttributeError):
                show = {}
            self._send(200, json.dumps(show))
        elif self.path == "/actions":
            self._send(200, json.dumps(read_actions()))
        elif self.path == "/debug":
            # The ui flags ride along here so "why did ⌘X not reach the browser?" is
            # answerable without a debugger — same reason the Dimension Value add-in
            # publishes its veto count.
            with _lock:
                d = dict(_debug)
                d["ui_flags"] = dict(_ui_flags)
                self._send(200, json.dumps(d))
        else:
            self.not_found()

    def do_POST(self):
        data = self.read_json_body()
        if data is None:
            return

        if self.path == "/uiflags":
            # The window telling us what it is holding. Posted only when it changes, so
            # this is a handful of requests per session rather than per poll.
            with _lock:
                had = _ui_flags["hasCut"]
                for k in ("canCut", "hasCut", "showOrigins", "showConstruction"):
                    if k in data:
                        _ui_flags[k] = bool(data[k])
                has = _ui_flags["hasCut"]
            if has != had:
                _note_cut(has)
            self._send(200, json.dumps({"ok": True}))
            return

        if self.path == "/busyspinner":
            # The menu tail's Busy Spinner tick: bin/fusion_busy stays quiet while
            # BUSY_OFF exists (checked twice a second), so the flag file IS the setting.
            try:
                if data.get("on"):
                    os.remove(BUSY_OFF)
                else:
                    open(BUSY_OFF, "w").close()
            except FileNotFoundError:
                pass
            self._send(200, json.dumps({"on": not os.path.exists(BUSY_OFF)}))
            return

        if self.path == "/uiaction":
            # Another add-in asking this window to run one of its actions. The Dimension
            # Value add-in uses it to file a parameter it just created beside the sketch
            # it belongs to: this window is the single writer of folders.json, so the
            # placement has to be made here rather than by a second process editing the
            # file underneath it.
            aid = str(data.get("id") or "")
            if not aid:
                self._send(400, json.dumps({"ok": False, "error": "id required"}))
                return
            alive = window_alive()
            if alive:
                push_ui_action(aid, data.get("arg"))
            self._send(200, json.dumps({"ok": True, "delivered": alive}))
            return

        if self.path == "/clipboard":
            # ⌘C on a parameter row: put its name on the SYSTEM clipboard, so pasting it
            # into fusion_value's expression field (or anywhere else) works. Done here,
            # not in the page — same reason as fusion_value's own /clipboard: a WKWebView
            # loaded over plain http can't rely on navigator.clipboard, and pbcopy is
            # already trusted with everything else this window does.
            text = data.get("text")
            if not isinstance(text, str) or not text:
                self._send(400, json.dumps({"ok": False, "error": "nothing to copy"}))
                return
            try:
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
                self._send(200, json.dumps({"ok": True}))
            except (OSError, subprocess.SubprocessError) as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return

        if self.path == "/lasterror":
            # The menu tail's Show Last Error: the HUD cross's message is only clickable
            # for the second it's up, so hud_flash keeps the last one and `last` shows it.
            # Detached — the alert is modal and lives as long as the user leaves it.
            try:
                subprocess.Popen([os.path.expanduser("~/bin/hud_flash"), "last"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return

        if self.path == "/cmd":
            action = data.get("action")
            if not action:
                self._send(400, json.dumps({"ok": False, "error": "action required"}))
                return
            _cmd_queue.put(data)
            self._send(200, json.dumps({"ok": True}))
            _wake_addin()
        elif self.path == "/folders":
            doc = str(data.get("docId") or "")
            if not doc:
                self._send(400, json.dumps({"ok": False, "error": "docId required"}))
                return
            try:
                _write_folders(doc, data.get("data") or {})
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif not self.serve_shared_post(data):
            self.not_found()


def make_server():
    return webwin.make_server(PORT, Handler)


if __name__ == "__main__":
    webwin.main("Fusion Browser", make_server, PORT)
