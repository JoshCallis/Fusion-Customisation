#!/usr/bin/env python3
"""
Fusion Timeline — local web server for the vertical timeline (port 8736).

The second panel of the left dock (FusionDock.app): the parametric timeline drawn
top-to-bottom in the Browser's black, borderless style, swapped with the Browser by
DockTimelineToggleCmd. Same shape as fusion_browser/server.py: the Timeline add-in
(AddIns/Timeline/Timeline.py) imports this module and, from its main-thread sync
handler, calls update_state() with the timeline rows and pop_commands() to drain what
the page posted. The HTTP server thread only ever touches the in-memory snapshot and
the command queue under a lock — never the Fusion API.

What this server owns on its own:
  /groups      the window-only user groups, per document (groups.json, like the
               Browser's folders.json). Fusion's own timeline groups are ignored.
               file_sketches() keeps every sketch in its component's "Sketches"
               group (called by the add-in — see its section below).
  /icon/<rel>  one Fusion toolbar icon, served out of the LIVE Fusion bundle — its
               hash changes with every update, so nothing is copied into the repo.
               icons.json maps an entity objectType to a resource dir; icon_for()
               picks the file (icons.json's `prefer` order).
  dockMode     which panel the dock shows ("browser" / "timeline"). It lives on
               builtins._fusion_dock (the Timeline add-in owns it), read here at
               serve time so both windows' /state carry the same answer.

Run standalone (`python3 fusion_timeline/server.py`) to style the page with no Fusion
attached: it serves test/fixture.json as the state.
"""

import builtins, importlib.util, json, os, queue, re, threading, time

PORT = 8736
HERE = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = os.path.join(os.path.dirname(HERE), "fusion_ui")
# The shared server plumbing (static + shared assets, /settings, CORS) — see
# fusion_ui/webwin.py's header for why it is loaded by path, fresh.
_spec = importlib.util.spec_from_file_location(
    "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)

GROUPS_FILE = os.path.join(HERE, "groups.json")
ICONS_FILE = os.path.join(HERE, "icons.json")
FIXTURE_FILE = os.path.join(HERE, "test", "fixture.json")


# ── The live Fusion bundle ────────────────────────────────────────────────────
# Resolved the way ~/Code/fusion-carryover/apply.sh does it: the launcher stub's
# destfolder (rewritten on every update), then the production/"Autodesk Fusion.app"
# symlink, then the newest production/<hash>.
LAUNCHER = os.path.expanduser(
    "~/Applications/Autodesk Fusion.app/Contents/MacOS/Autodesk Fusion")
PROD = os.path.expanduser("~/Library/Application Support/Autodesk/webdeploy/production")
RES_REL = ("Contents/Libraries/Applications/Fusion/Fusion/UI/FusionUI/Resources")

_res_cache = {"at": 0.0, "root": ""}


def _resolve_app():
    try:
        with open(LAUNCHER, encoding="utf-8", errors="replace") as f:
            m = re.search(r'destfolder="([^"]+)"', f.read())
        if m:
            app = os.path.join(m.group(1), "Autodesk Fusion.app")
            if os.path.isdir(app):
                return app
    except OSError:
        pass
    link = os.path.join(PROD, "Autodesk Fusion.app")
    if os.path.exists(link):
        return os.path.realpath(link)
    newest, stamp = "", 0.0
    try:
        for d in os.listdir(PROD):
            app = os.path.join(PROD, d, "Autodesk Fusion.app")
            if os.path.isdir(app):
                t = os.path.getmtime(app)
                if t > stamp:
                    newest, stamp = app, t
    except OSError:
        pass
    return newest


def resources_root():
    """Fusion's UI Resources dir, re-resolved at most once a minute (an update moves it)."""
    now = time.time()
    if not _res_cache["root"] or now - _res_cache["at"] > 60:
        app = _resolve_app()
        root = os.path.join(app, RES_REL) if app else ""
        _res_cache.update(at=now, root=root if root and os.path.isdir(root) else "")
    return _res_cache["root"]


# ── Icons ─────────────────────────────────────────────────────────────────────
_icons = {"mtime": -1.0, "data": {}}
_icon_pick = {}      # resource dir -> chosen file rel path ("" = none there)


def _icon_table():
    try:
        m = os.path.getmtime(ICONS_FILE)
    except OSError:
        return {}
    if m != _icons["mtime"]:
        _icons["data"] = webwin.read_json(ICONS_FILE, {})
        _icons["mtime"] = m
        _icon_pick.clear()
    return _icons["data"]


def _pick_file(rdir):
    """The best file in one resource dir, by icons.json's `prefer` order."""
    if rdir in _icon_pick:
        return _icon_pick[rdir]
    root = resources_root()
    got = ""
    if root:
        for name in _icon_table().get("prefer") or ["16x16.png"]:
            if os.path.isfile(os.path.join(root, rdir, name)):
                got = rdir + "/" + name
                break
    if root:                      # never cache a miss made with no bundle at all
        _icon_pick[rdir] = got
    return got


def icon_for(object_type, hint=""):
    """Rel path of the icon for an entity objectType ("" = the page draws its generic
    glyph). `hint` is a finer key the add-in may pass (e.g. a sketch that is a Bounds
    sketch) — looked up first, in the same table."""
    table = _icon_table()
    types = table.get("types") or {}
    for key in (hint, object_type):
        if key and key in types:
            got = _pick_file(types[key])
            if got:
                return got
    fb = table.get("fallback") or ""
    return _pick_file(fb) if fb else ""


def _serve_icon(handler, rel):
    root = resources_root()
    if not root:
        return handler.not_found()
    path = os.path.realpath(os.path.join(root, rel))
    # Only ever a file inside Resources — the page is the only caller, but a path that
    # climbs out of the bundle is not something to answer.
    if not path.startswith(os.path.realpath(root) + os.sep):
        return handler.not_found()
    ctype = "image/svg+xml" if path.endswith(".svg") else "image/png"
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        return handler.not_found()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    # Icons do not change under a given path until the bundle does — and then the
    # path carries the new file anyway. Let WebKit keep them.
    handler.send_header("Cache-Control", "max-age=3600")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


# ── Window-only groups, per document ──────────────────────────────────────────
# Two pages edit them: this one, and the Browser (a body's feature rows file into the
# same groups). Each holds a copy, so every write bumps `_groups_rev`, carried on
# /state and /feats; a page seeing a rev it didn't write reloads before its next save
# could write its stale copy back.
_groups_lock = threading.RLock()     # re-entered by file_sketches → _write_groups
_groups_rev = 0


def _read_groups():
    try:
        with open(GROUPS_FILE) as f:
            return f.read()
    except OSError:
        return json.dumps({"byDoc": {}})


def _write_groups(doc_id, data):
    """Replace one document's groups, leaving every other document's alone. One step
    back is kept in groups.json.bak before any write that changes something — the
    file is the only copy of the groups, same reasoning as the Browser's folders."""
    with _groups_lock:
        try:
            with open(GROUPS_FILE) as f:
                raw = f.read()
            allg = json.loads(raw)
        except (OSError, ValueError):
            raw, allg = "", {}
        if not isinstance(allg, dict):
            allg = {}
        by_doc = allg.get("byDoc")
        if not isinstance(by_doc, dict):
            by_doc = allg["byDoc"] = {}
        global _groups_rev
        if raw and by_doc.get(doc_id) == data:
            return _groups_rev
        if raw:
            try:
                webwin.atomic_write(GROUPS_FILE + ".bak", raw)
            except OSError:
                pass
        by_doc[doc_id] = data
        webwin.atomic_write(GROUPS_FILE, json.dumps(allg, indent=2))
        _groups_rev += 1
        return _groups_rev


# ── Every sketch in its component's Sketches group ────────────────────────────
# The Timeline add-in calls file_sketches() whenever the timeline or the groups change
# (panel shown or not), with every component's sketch row keys. Each component that has
# sketches gets a "Sketches" group at its top; a sketch filed nowhere goes into it — a
# bounds sketch (Bounds.py's "Bounds X/Y/Z", legacy "Bounds XY/XZ/YZ") into a "Bounds"
# group inside it. Sketches already in some other group of their component stay put.
# Once per document (`sketchGroups` flag) the groups made by hand before this existed
# are tidied in too: a top-level group holding only sketches moves inside Sketches, and
# an unnamed one holding only bounds sketches is named "Bounds".
SKETCHES_GROUP = "Sketches"
BOUNDS_GROUP = "Bounds"
PLANES_GROUP = "Planes"
_AUTO_NAME = re.compile(r"^(Group( \d+)?)?$")      # tl_names.js's AUTO_NAME


def _group_comp(groups, gid):
    """tl_groups.js's groupComp: the compRef a group's parent chain ends at, or None."""
    seen = set()
    while gid in groups and gid not in seen:
        seen.add(gid)
        p = str(groups[gid].get("parent") or "")
        if p.startswith("c:"):
            return p[2:]
        if not p.startswith("g:"):
            return None
        gid = p[2:]
    return None


def _new_group(groups, name, parent):
    n = 1
    while "g%d" % n in groups:
        n += 1
    gid = "g%d" % n
    groups[gid] = {"name": name, "parent": parent, "collapsed": True}
    return gid


def _named(g, name):
    return str(g.get("name") or "").strip().lower() == name.lower()


def sketch_groups(data, sketches, migrate):
    """Pure: file `sketches` ({compRef: {rowKey: is_bounds}}) into `data` (one
    document's groups, edited in place). True when anything changed."""
    groups = data.setdefault("groups", {})
    member = data.setdefault("member", {})
    changed = False
    for ref, keys in sketches.items():
        if not keys:
            continue
        cp = "c:" + ref

        def filed(k):
            gid = member.get(k)
            return gid in groups and _group_comp(groups, gid) == ref

        top = sorted((gid for gid, g in groups.items() if g.get("parent") == cp),
                     key=lambda s: (len(s), s))
        home = next((gid for gid in top if _named(groups[gid], SKETCHES_GROUP)), None)

        if migrate:
            for gid in top:
                if gid == home or _named(groups[gid], PLANES_GROUP):
                    continue
                sub, stack = set(), [gid]
                while stack:
                    s = stack.pop()
                    if s in sub:
                        continue
                    sub.add(s)
                    stack += [c for c, g in groups.items() if g.get("parent") == "g:" + s]
                rows = [k for k, v in member.items() if v in sub]
                if not rows or any(k not in keys for k in rows):
                    continue
                if home is None:
                    home = _new_group(groups, SKETCHES_GROUP, cp)
                groups[gid]["parent"] = "g:" + home
                if all(keys[k] is True for k in rows) and _AUTO_NAME.match(
                        str(groups[gid].get("name") or "").strip()):
                    groups[gid]["name"] = BOUNDS_GROUP
                changed = True

        loose = [k for k in keys if not filed(k)]
        if not loose:
            continue
        if home is None and any(keys[k] != "plane" for k in loose):
            home = _new_group(groups, SKETCHES_GROUP, cp)
        bounds = planes = None
        for k in loose:
            if keys[k] == "plane":
                # Construction planes sit in the component's own "Planes" group, a
                # sibling of Sketches (a plane is not a sketch).
                if planes is None:
                    planes = next((gid for gid in top if _named(groups[gid], PLANES_GROUP)),
                                  None) or _new_group(groups, PLANES_GROUP, cp)
                member[k] = planes
            elif keys[k]:
                if bounds is None:
                    bounds = next((gid for gid, g in sorted(groups.items())
                                   if g.get("parent") == "g:" + home
                                   and _named(g, BOUNDS_GROUP)), None)
                    if bounds is None:
                        bounds = _new_group(groups, BOUNDS_GROUP, "g:" + home)
                member[k] = bounds
            else:
                member[k] = home
        changed = True
    return changed


def file_sketches(doc_id, sketches):
    """Apply sketch_groups to one document's stored groups. Returns the groups rev."""
    if not doc_id:
        return _groups_rev
    with _groups_lock:
        try:
            with open(GROUPS_FILE) as f:
                allg = json.load(f)
        except (OSError, ValueError):
            allg = {}
        data = ((allg.get("byDoc") if isinstance(allg, dict) else None) or {}).get(doc_id)
        data = json.loads(json.dumps(data)) if isinstance(data, dict) else {}
        for k in ("groups", "member", "compCollapsed"):
            if not isinstance(data.get(k), dict):
                data[k] = {}
        migrate = not data.get("sketchGroups")
        changed = sketch_groups(data, sketches, migrate)
        if migrate:
            data["sketchGroups"] = 1
            changed = True
        return _write_groups(doc_id, data) if changed else _groups_rev


# ── State ─────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
# rows      the timeline, in order: {key, tok, name, type, icon, compRef, compPath,
#           health, msg, suppressed, past, sketch, occTok}
# comps     {compRef: {name, path, occTok}} for every component in the active subtree
# activeRef the active component's ref (Component.id) — the list's top level
# locked    the lock is holding the active component (timeline mode is on)
_state = {"ts": 0, "doc_id": "", "rows": [], "comps": {}, "activeRef": "",
          "activeName": "", "locked": False, "editing": False,
          "uiActions": [], "uiSeq": 0}
# The window outlives this server, so its counter starts from the clock — see
# webwin.seq_base.
_state["uiSeq"] = webwin.seq_base()
_cmd_queue: queue.Queue = queue.Queue()
_debug = {"last_sync_ts": 0, "rows": 0, "last_error": "", "walks": 0}

STANDALONE = False     # set by __main__: /state then reports timeline mode


def set_debug(**kw):
    with _lock:
        _debug.update(kw)


def update_state(rows, meta=None):
    """Called from the Fusion main thread with the timeline rows."""
    with _lock:
        _state["rows"] = rows
        for k, v in (meta or {}).items():
            _state[k] = v
        _state["ts"] = int(time.time() * 1000)
        _debug["last_sync_ts"] = _state["ts"]
        _debug["rows"] = len(rows)


def patch_state(**kw):
    """A few fields without the rows (lock/editing flags between walks)."""
    with _lock:
        _state.update(kw)


# The Browser page's feature rows (a component's timeline features, drawn in its
# Geometry group when that is opened). Kept apart from _state: the Browser polls /feats,
# not /state, and only while a Geometry group is open.
#   feats   {fx:<comp id>: [{tok, key, compRef, name, type, icon, phase, health, msg,
#            suppressed, past, unseen}]} — `key` is the timeline row's own, for its groups
#   doc_id  the document they belong to (the groups are per document)
#   probe   the answer to the Browser's last `featprobe` ({id, ok, after, why})
#   notice  {at, text} — a refused move / a placement report, flashed by the page
#   reorder {id, text} — the verdict on the Browser's last `featreorder` ('' = moved),
#            published with the rows walked after it so the page swaps both at once
_feats = {"feats": {}, "probe": None, "notice": None, "phases": [], "doc_id": ""}
_last_feats_poll = 0.0


def patch_feats(**kw):
    with _lock:
        _feats.update(kw)


def feats_alive(max_age=2.0):
    """True while the Browser page is polling /feats (a Geometry group is open there)."""
    return (time.time() - _last_feats_poll) < max_age


def dock_mode():
    """Which panel the dock is showing. Owned by the Timeline add-in, on builtins."""
    d = getattr(builtins, "_fusion_dock", None)
    if isinstance(d, dict) and d.get("mode") in ("browser", "timeline"):
        return d["mode"]
    return "timeline" if STANDALONE else "browser"


_ui_seq = _state["uiSeq"]
UI_ACTION_KEEP = 20
_last_poll = 0.0


def window_alive(max_age=2.0):
    """True if the page has polled /state recently — it polls every 80 ms while open."""
    return (time.time() - _last_poll) < max_age


def push_ui_action(action_id, arg=None):
    """Queue an action for the PAGE to run — the Browser's hotkeys arrive here while
    timeline mode is on (see HotkeyStartingHandler in Browser.py). Same contract as
    the Browser server's: a monotonic seq, trimmed, adopted on the page's first poll."""
    global _ui_seq
    with _lock:
        _ui_seq += 1
        kept = (_state.get("uiActions") or [])[-(UI_ACTION_KEEP - 1):]
        _state["uiActions"] = kept + [{"seq": _ui_seq, "id": action_id, "arg": arg}]
        _state["uiSeq"] = _ui_seq


def pop_commands():
    return webwin.drain(_cmd_queue)


class Handler(webwin.Handler):
    HERE = HERE

    def do_GET(self):
        if self.serve_static():
            return
        route = self.route
        if route == "/state":
            global _last_poll
            _last_poll = time.time()
            with _lock:
                s = dict(_state)
            s["dockMode"] = dock_mode()
            s["groupsRev"] = _groups_rev
            self.send_json(s)
        elif route == "/feats":
            global _last_feats_poll
            _last_feats_poll = time.time()
            with _lock:
                f = dict(_feats)
            f["groupsRev"] = _groups_rev
            self.send_json(f)
        elif route == "/groups":
            self._send(200, _read_groups())
        elif route.startswith("/icon/"):
            _serve_icon(self, route[len("/icon/"):])
        elif route == "/debug":
            with _lock:
                d = dict(_debug)
            d["resources"] = resources_root()
            d["dockMode"] = dock_mode()
            self.send_json(d)
        else:
            self.not_found()

    def do_POST(self):
        data = self.read_json_body()
        if data is None:
            return
        route = self.route
        if route == "/cmd":
            if not data.get("action"):
                return self.fail(400, "action required")
            _cmd_queue.put(data)
            self.ok()
        elif route == "/groups":
            doc = str(data.get("docId") or "")
            if not doc:
                return self.fail(400, "docId required")
            try:
                self.ok(rev=_write_groups(doc, data.get("data") or {}))
            except OSError as e:
                self.fail(500, e)
        elif not self.serve_shared_post(data):
            self.not_found()


def make_server():
    return webwin.make_server(PORT, Handler)


def _load_fixture():
    fx = webwin.read_json(FIXTURE_FILE, {})
    rows = fx.get("rows") or []
    for r in rows:
        if not r.get("icon"):
            r["icon"] = icon_for(r.get("type", ""))
    meta = {k: v for k, v in fx.items() if k not in ("rows", "comment")}
    meta.setdefault("compIcon", icon_for("adsk::fusion::Occurrence"))
    update_state(rows, meta)


if __name__ == "__main__":
    STANDALONE = True
    _load_fixture()
    webwin.main("Fusion Timeline", make_server, PORT)
