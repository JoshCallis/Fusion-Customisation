"""
Fusion Shortcuts — add-in.

Same shape as the Params / Browser companion add-ins: a local HTTP server
(server.py) in a background thread, a custom event so every Fusion API call lands
on the main thread, and a toolbar button that toggles FusionShortcuts.app.

What this one contributes that the window can't get for itself:

  1. The command CATALOG. Fusion's keymap file stores bare command ids
     ("FusionSheetMetalFlangeCommand"); only the running app knows the readable
     name and where the command lives. We walk every workspace → tab → panel →
     control once and record id → (name, tooltip, workspace/tab group, panel).
     That walk is what gives the window its organisation, so it isn't one flat
     list. Rebuilt on request (the window asks when it opens), never per tick.

  2. RECENTS. Fusion's own shortcut panel offers "recently used commands" to
     assign keys to; nothing exposes that list, so we keep our own by listening
     to ui.commandStarting. Persisted to disk so it survives a restart.

  3. WRITING a binding. Fusion has no hotkey API — nothing in API/CPP/include
     mentions shortcuts, and none of its ~3100 command definitions is an assign
     hook. What it does have is four undocumented text commands in a "HotKey"
     subject area (found in NuBase10.dylib as TextCmdHotKey{List,Dialog,Export,
     Import}). All four were probed against a live Fusion:

         HotKey.List      WORKS. Returns the whole live keymap as JSON.
         HotKey.Dialog    WORKS, and takes a command id: `HotKey.Dialog Extrude`
                          opens Fusion's own modal "Change Keyboard Shortcut"
                          prompt bound to that command. Verified by diffing
                          HotKey.List either side of a keypress: the key landed on
                          the command passed in. (An earlier note here said the
                          prompt does not name the command. It does — read off its
                          accessibility tree 2026-09-08: "Assign a new shortcut key
                          for Browser: Settings:". It also says "Leave blank to
                          remove the shortcut", which is the only way to clear one
                          in Fusion.)
         HotKey.Export    Returns "SUCCESS." and does nothing — ignores any path
                          argument and writes no file.
         HotKey.Import    Same, "SUCCESS." with no effect, even for a path that
                          cannot exist.

     So List is the read path and Dialog is the write path. Assigning hands off to
     Fusion's own prompt, which means Fusion — not us — handles conflicts,
     reserved keys, applying the binding live and persisting it. The options XML
     is never edited while Fusion runs: the running app never re-reads it and
     rewrites it itself, so an edit would be both ignored and lost. Control Room's
     Apply: On Restart queues changes instead and ~/bin/fusion-restart writes them
     into the XML while Fusion is closed; _push_live_keys replays that queue on
     HotKey.List (keymap.overlay) so nothing here undoes it before then. See
     fusion_ui/keymap.py and remap/README.md § Apply on restart.
"""

import adsk.core, adsk.fusion
import importlib.util, json, os, subprocess, sys, threading, time, traceback
import types as _types
import urllib.error, urllib.request

# The process-wide "someone is already in the model" latch — read its header before
# changing anything about the sync tick. It is why the sync below can decline to run.
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
tickguard = _load_local('tickguard', 'Tick Guard.py')

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

CMD_ID   = 'FusionShortcutsCmd'
CMD_NAME = 'Shortcuts'
CMD_DESC = 'Browse, search and set keyboard shortcuts'
PANEL_ID = 'SolidScriptsAddinsPanel'
EVENT_ID = 'FusionShortcutsSyncEvent'

EDITOR_DIR = '/Users/josh/Code/Scripts/fusion_shortcuts'
SERVER_PY  = os.path.join(EDITOR_DIR, 'server.py')
RECENTS_F  = os.path.join(EDITOR_DIR, 'recents.json')
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
APP_PATH   = '/Applications/FusionShortcuts.app'
APP_PROC   = 'FusionShortcuts'

# The command-search palette. Same window as the dimension-value dialog (fusion_value,
# port 8734) — its 'search' mode — not a window of its own; see fusion_value/CLAUDE.md.
SEARCH_CMD_ID = 'CommandSearchCmd'
JUMP_CMD_ID   = 'JumpToNameCmd'
GOTO_CMD_ID   = 'GoToCmd'
JUMP_URL      = 'http://127.0.0.1:8741'
JUMP_OPEN_SH  = '/Users/josh/Code/Scripts/jump/open.sh'
VALUE_URL     = 'http://127.0.0.1:8734'
# Control Room's own server — the palette's per-row sheet sets a shortcut through it
# (POST .../fusion/set), same as Remap.app's own page. That app normally starts this
# server itself when its window opens; _ensure_remap_server below covers the case
# where the sheet is opened before Remap.app ever has been.
REMAP_URL       = 'http://127.0.0.1:8735'
REMAP_SERVER_PY = '/Users/josh/Code/Scripts/remap/server.py'

MAX_RECENTS = 60

# Commands that fire constantly as a side effect of ordinary interaction. They'd
# swamp the recents list and none of them is something you'd bind a key to.
RECENT_SKIP = {
    'SelectCommand', 'CommitCommand', 'FusionCancelCommand', 'EscapeCommand',
    'RepeatCommand', 'PanCommand', 'OrbitCommand', 'ZoomCommand', 'FitCommand',
    'LookAtCommand', 'SelectionCommand', 'DragCommand', 'FusionRefreshCommand',
    CMD_ID,
}

_httpd      = None
_server_mod = None
_keymap_mod = None
_stop_evt   = threading.Event()
_custom_evt = None
_active     = False

_catalog_built = False
_last_keys_push = 0.0
KEYS_PUSH_EVERY = 1.0     # seconds between HotKey.List refreshes
_recents       = []     # [{'id':..., 'ts':...}] most-recent-first
_recents_dirty = False
_last_recent_id = ''

# ── The action registry ────────────────────────────────────────────────────────

def _hotkeys():
    """The shared action registry, via sys.modules: ONE registry and ONE router for
    every add-in. Loading the file directly would give each add-in a private copy and
    the whole thing would quietly stop routing.

    It also hot-reloads itself. hotkeys.py lives outside the AddIns folder, so Reload
    Recent AddIns cannot map it to its consumers, and the sys.modules cache would
    otherwise pin the old engine until Fusion restarted. Re-exec is into the SAME
    __dict__, which is exactly what hotkeys.py's globals() recovery is built for — the
    registry and the live commandStarting registration both survive it.
    """
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod      # before exec: the module may re-enter
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod


def _push_registry():
    """Mirror the registry — actions, domains, assignments, live context — to the
    server so the window can draw it beside Fusion's own keymap."""
    try:
        _load_server().update_registry(_hotkeys().snapshot())
    except Exception:
        pass


# ── Command search ───────────────────────────────────────────────────────────────
#
# Taking S over from Fusion's native command search needs no router trickery: assigning
# ANY of our own declared commands a key, through this window's ordinary "assign a
# shortcut" flow, drives Fusion's real HotKey.Dialog on that id (see _open_shortcut_
# dialog and the module docstring) — the one true write path, which replaces whatever
# was on that physical key before at the Fusion level, same as reassigning any native
# shortcut. CommandSearchCmd only has to exist as a declared action (below) so the
# window lists it — under "My add-ins" — as something to assign, same as any other.
#
# An earlier version of this tried to fake the takeover by writing a claim into
# fusion_ui/bindings.json directly and giving it priority in the router. That fought
# sync_from_keymap's own rule that Fusion's keyboard panel is the legitimate front
# door: the moment CommandSearchCmd got a REAL assignment through the window (exactly
# what happened testing this — it landed on 'i'), sync_from_keymap re-synced the fake
# 's' claim back to 'i' on the very next tick, silently. Don't reintroduce that; assign
# it through the window instead, the same as everything else here.

def _ensure_remap_server():
    """Spawn remap/server.py unless something is already answering as ours — the same
    marker check RemapApp/main.swift's startServerIfNeeded uses: 8730-8734 are the
    other companion servers and they all answer /health too, so a bare 200 here proves
    only that the port is busy (see the comment on remap/server.py's own /health
    handler — this is that exact trap). Control Room's own window normally
    starts this server when it opens; the palette's per-row sheet needs it every time
    it sets or clears a shortcut, so this covers opening the sheet before Remap.app
    ever has been. Connection-refused on localhost is immediate, so the added
    main-thread cost here is one Popen when it's down, and none at all when it's up."""
    try:
        body = urllib.request.urlopen(REMAP_URL + '/health', timeout=1).read()
        if b'"remap"' in body:
            return
    except Exception:
        pass
    try:
        subprocess.Popen(['/usr/bin/python3', REMAP_SERVER_PY],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _open_jump():
    """JumpToNameCmd: the invisible Jump to Name box (Scripts/jump/) for Fusion — type
    or dictate a component / Browser folder name, Enter goes there. Off the main thread:
    a POST to the jump server, or open.sh when it's down (a fork — rare, and never on
    Fusion's main thread)."""
    def go():
        try:
            req = urllib.request.Request(JUMP_URL + '/open', data=b'{"app": "fusion"}',
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            try:
                subprocess.Popen([JUMP_OPEN_SH, 'fusion'], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass
    threading.Thread(target=go, daemon=True).start()


def _open_goto():
    """GoToCmd (Universal Go To, Scripts/jump/goto.sh): the jump box in voice mode, then
    Wispr Flow — the spoken component / folder / body is gone to. Two POSTs off the main
    thread (the jump server's /dictate taps Wispr's key); open.sh only when it's down."""
    def post(path, body):
        req = urllib.request.Request(JUMP_URL + path, data=body,
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=1).read()

    def go():
        try:
            post('/open', b'{"app": "fusion", "voice": true}')
            time.sleep(0.3)
            post('/dictate', b'{}')
        except Exception:
            try:
                subprocess.Popen(['/Users/josh/Code/Scripts/jump/goto.sh', 'fusion'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    threading.Thread(target=go, daemon=True).start()


def _open_search():
    """The router's run callback for CommandSearchCmd, once it is bound to a key
    through this window. Runs on Fusion's main thread, from inside commandStarting, so
    a bounded local POST here is the same trade _open_shortcut_dialog already makes for
    the far longer native shortcut-assign modal — this fires once per keypress, not
    once per frame the way Extrude's own network calls would if they weren't kept off
    the main thread.

    A 200 back from /search/open does NOT mean the palette appeared. That server lives
    in the Dimension Value add-in and answers whether or not the WINDOW process is
    healthy, so the POST cannot fail the way it would if the two shared a process — and
    the heal below therefore used to be unreachable. It cost a day: the key looked dead
    while every layer behind it (router, keymap, server, page) tested fine, because a
    FusionValue instance that was up but no longer polling armed session after session
    that nothing ever rendered. Every opener inside "Dimension Value.py" calls
    _ensure_window() before arming for exactly this reason; this one is a separate
    add-in and can only ask, so it asks on the reply's `windowAlive` instead.

    `env` is sampled here, on the way past, because only Fusion's own process can read
    it (fusion_ui/hotkeys.py's derived 'env' axis) — nothing publishes it outside that
    process, so it has to ride along on THIS post rather than be looked up later by
    whatever renders the palette.

    The disabled set is sampled here too, for the same reason: the catalog holds every
    workspace's commands, and one Fusion reports disabled (Drawing's "CircleDiameter",
    Electronics' "Circle" while in a Design sketch) does nothing on execute() — picking
    it looked like a dead search. Walking all ~3,200 definitions measured ~30 ms. It is
    set on this add-in's own server BEFORE the POST, so the palette's /data fetch after
    the open always sees it."""
    env = _hotkeys().context(['env']).get('env', '')
    try:
        disabled = []
        cds = ui.commandDefinitions
        for i in range(cds.count):
            try:
                cd = cds.item(i)
                if not cd.controlDefinition.isEnabled:
                    disabled.append(cd.id)
            except Exception:
                pass
        _load_server().set_disabled(disabled)
    except Exception:
        pass                 # unknown = the palette offers everything, as before
    alive = False
    try:
        req = urllib.request.Request(
            VALUE_URL + '/search/open', data=json.dumps({'env': env}).encode(),
            headers={'Content-Type': 'application/json'})
        body = urllib.request.urlopen(req, timeout=1).read()
        # A server too old to report it is assumed healthy: the old behaviour, rather
        # than a heal — and a pkill — on every single keypress.
        alive = bool(json.loads(body.decode() or '{}').get('windowAlive', True))
    except Exception:
        alive = False        # server down, or it answered something unparseable
    # AFTER the open, not before: only the palette's per-row sheet needs Control Room,
    # and a health check (or a Popen) ahead of the POST was pure delay between the key
    # and the palette — you start typing straight after S.
    _ensure_remap_server()
    if alive:
        return
    # Down or wedged. Ask Dimension Value's tick to heal it — it owns the process (see
    # _ensure_window in "Dimension Value.py"). The session is already armed by now, and
    # that is deliberate: the relaunched page adopts an open session on its first poll,
    # so THIS keypress still lands rather than being spent on the repair.
    try:
        req = urllib.request.Request(
            VALUE_URL + '/cmd', data=json.dumps({'action': 'ensurewindow'}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=1).read()
    except Exception:
        pass


# ── Server helpers ─────────────────────────────────────────────────────────────

def _load_server():
    global _server_mod
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('fusion_shortcuts_server', SERVER_PY)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, 'set_wake'):
            mod.set_wake(_wake_tick)
        _server_mod = mod
    return _server_mod


def _wake_tick():
    """Called on the server's thread the moment /run queues a command, so it runs
    now rather than on the next poll. Same no-stacking rule as _poller."""
    global _tick_pending
    if _tick_pending:
        return
    _tick_pending = time.monotonic()
    try:
        app.fireCustomEvent(EVENT_ID, '')
    except Exception:
        _tick_pending = 0.0


def _reclaim_port(port):
    """Shut down any server in THIS process still bound to `port`. A reload that lost
    its `_httpd` reference (the module re-executed without stop() running) leaves the
    old server thread serving the old server.py forever — no syncs reach it, and every
    later Run just hit "port taken" and left it there. It's in-process, so gc finds it."""
    import gc, socketserver
    for o in gc.get_objects():
        if not isinstance(o, socketserver.BaseServer) or o is _httpd:
            continue
        try:
            if o.server_address[1] != port:
                continue
            # shutdown() blocks until serve_forever returns — only call it on one
            # that is actually serving.
            if not o._BaseServer__is_shut_down.is_set():
                o.shutdown()
            o.server_close()
        except Exception:
            pass


def _start_server():
    global _httpd
    mod = _load_server()
    if _httpd is None:
        for attempt in (0, 1):
            try:
                _httpd = mod.make_server()
                threading.Thread(target=_httpd.serve_forever, daemon=True).start()
                return
            except OSError:
                _httpd = None
                if attempt == 0:
                    _reclaim_port(mod.PORT)   # an orphan of ours, not another process


def _stop_server():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None


# ── Command catalog ────────────────────────────────────────────────────────────

# Panels whose contents are add-ins/scripts rather than stock Fusion commands.
ADDIN_PANEL_IDS = {'SolidScriptsAddinsPanel', 'thomasa88_anyShortcutPanel'}


def _control_defs(controls, out, group, panel_name, kind, depth=0):
    """Collect every CommandDefinition reachable from a ToolbarControls collection.
    Drop-downs nest (Create ▸ Pattern ▸ Rectangular Pattern), so recurse — but cap
    the depth, since a malformed control tree would otherwise loop."""
    if depth > 6:
        return
    try:
        count = controls.count
    except Exception:
        return
    for i in range(count):
        try:
            ctrl = controls.item(i)
        except Exception:
            continue
        try:
            drop = adsk.core.DropDownControl.cast(ctrl)
        except Exception:
            drop = None
        if drop:
            # A drop-down's own name is a better panel label for its children than
            # the panel's ("Modify ▸ Change Parameters"), so pass it down.
            try:
                sub = drop.name or panel_name
            except Exception:
                sub = panel_name
            _control_defs(drop.controls, out, group, sub, kind, depth + 1)
            continue
        try:
            cc = adsk.core.CommandControl.cast(ctrl)
            if not cc:
                continue
            cd = cc.commandDefinition
            if not cd:
                continue
            cid = cd.id
        except Exception:
            continue
        if not cid or cid in out:
            continue
        out[cid] = {
            'id': cid,
            'name': _clean_name(_safe(cd, 'name')) or cid,
            'tooltip': _safe(cd, 'tooltip'),
            'group': group,
            'panel': panel_name,
            'kind': kind,
            'local': _local(cd),
        }


def _local(cd):
    """Made through the API by an add-in in this AddIns folder, as far as Fusion can
    say: not native, and no icons from anywhere else (Fusion's InternalAddins, the
    ApplicationPlugins bundles). Whether the add-in is OURS is remap/fusion.py's call
    (its id must be written in our git-tracked source) — this only rules out the rest."""
    try:
        if cd.isNative:
            return False
    except Exception:
        return False
    try:
        rf = cd.resourceFolder or ''
    except Exception:
        rf = ''                       # ours mostly have none, and the getter throws
    return not rf or os.path.realpath(rf).startswith(os.path.realpath(_ADDINS_DIR) + os.sep)


def _safe(obj, attr):
    try:
        return getattr(obj, attr) or ''
    except Exception:
        return ''


def _clean_name(name):
    """Undo Qt's mnemonic escaping in a command's display name.

    Fusion's names come through with '&' doubled ("Hide && Deselect"), because in Qt
    a single '&' marks the next character as the menu accelerator and '&&' is how you
    write a literal one. Only the doubled form is collapsed: stripping lone '&'s as
    accelerator markers would be guesswork, and it would quietly mangle any name that
    legitimately contains one. Nothing in the current catalog has a lone '&' anyway.
    """
    return name.replace('&&', '&') if name else name


def _build_catalog():
    """Walk the whole UI once. Returns (commands list, ordered group labels)."""
    out    = {}
    groups = []

    def add_group(g):
        if g not in groups:
            groups.append(g)

    # Workspaces → tabs → panels. The tab is the real category ("SOLID", "SKETCH",
    # "Milling") and is what the window groups by.
    #
    # Fusion SHARES tab objects between workspaces — the same SKETCH tab is listed
    # by Design, Form, Surface, Sheet Metal, PCB, Package, ParaMesh and more, so
    # walking workspace×tab produced 220 groups for 65 real ones, most of them
    # duplicates of each other. Keying on tab id collapses them to one. A tab name
    # that genuinely belongs to two different tabs (UTILITIES in Design vs in the
    # Electronics library) is disambiguated with its workspace.
    seen_tabs = {}          # tab id -> group label
    label_owner = {}        # group label -> the tab id that claimed it
    try:
        ws_count = ui.workspaces.count
    except Exception:
        ws_count = 0
    for w in range(ws_count):
        try:
            ws = ui.workspaces.item(w)
            ws_name = ws.name or ws.id
        except Exception:
            continue
        try:
            tab_count = ws.toolbarTabs.count
        except Exception:
            continue
        for t in range(tab_count):
            try:
                tab = ws.toolbarTabs.item(t)
                tab_id = tab.id or ''
                tab_name = tab.name or tab_id
            except Exception:
                continue
            if tab_id and tab_id in seen_tabs:
                continue                       # same tab, already walked
            label = tab_name
            if label_owner.get(label, tab_id) != tab_id:
                label = '%s ▸ %s' % (ws_name, tab_name)
            seen_tabs[tab_id] = label
            label_owner.setdefault(label, tab_id)

            try:
                panel_count = tab.toolbarPanels.count
            except Exception:
                continue
            for p in range(panel_count):
                try:
                    panel = tab.toolbarPanels.item(p)
                    panel_name = panel.name or panel.id
                    panel_id = panel.id or ''
                except Exception:
                    continue
                kind = 'addin' if panel_id in ADDIN_PANEL_IDS else 'command'
                group = 'My add-ins' if kind == 'addin' else label
                add_group(group)
                try:
                    _control_defs(panel.controls, out, group, panel_name, kind)
                except Exception:
                    pass

    # Free-floating toolbars: quick-access bar, navigation bar.
    try:
        tb_count = ui.toolbars.count
    except Exception:
        tb_count = 0
    for i in range(tb_count):
        try:
            tb = ui.toolbars.item(i)
            label = {'QAT': 'Quick access', 'NavToolbar': 'Navigation bar'}.get(
                tb.id, tb.id or 'Toolbar')
        except Exception:
            continue
        add_group('Application')
        try:
            _control_defs(tb.controls, out, 'Application', label, 'command')
        except Exception:
            pass

    # Everything else Fusion knows about but doesn't show in a panel — workspace
    # switches, dialog-only commands, add-in commands that never got a button.
    # Only worth listing if it's nameable or already bound, otherwise the "all
    # commands" view fills with internal machinery.
    bound = set()
    try:
        for entry in _load_server().read_hotkeys():
            for c in entry['commands']:
                bound.add(c['id'])
    except Exception:
        pass
    recent_ids = {r['id'] for r in _recents}

    try:
        cd_count = ui.commandDefinitions.count
    except Exception:
        cd_count = 0
    other = False
    for i in range(cd_count):
        try:
            cd = ui.commandDefinitions.item(i)
            cid = cd.id
        except Exception:
            continue
        if not cid or cid in out or cid.startswith('JoshRoute_'):
            continue        # a route is machinery — Fusion's own dialog is the only
                             # place it should ever show up
        name = _safe(cd, 'name')
        if not name and cid not in bound and cid not in recent_ids:
            continue
        other = True
        out[cid] = {
            'id': cid,
            'name': _clean_name(name) or cid,
            'tooltip': _safe(cd, 'tooltip'),
            'group': 'Other commands',
            'panel': '',
            'kind': 'other',
            'local': _local(cd),
        }
    if other:
        add_group('Other commands')

    # A binding can point at a command this Fusion session doesn't have loaded
    # (an add-in that isn't running, or a workspace you don't have). Still show
    # it — a shortcut you can't identify is exactly what this window is for.
    missing = False
    for cid in bound:
        if cid in out or cid.startswith('JoshRoute_'):
            continue
        missing = True
        out[cid] = {
            'id': cid,
            'name': cid,
            'tooltip': '',
            'group': 'Not loaded',
            'panel': '',
            'kind': 'missing',
        }
    if missing:
        add_group('Not loaded')

    # Declared actions are ours by definition, whether or not they ever got a button.
    # Group them under the add-in that owns them so the window lists "Extrude" with
    # its own actions rather than burying them in "Other commands".
    try:
        for a in _hotkeys().actions():
            if a.native:
                continue
            entry = out.get(a.id) or {'id': a.id, 'tooltip': ''}
            entry.update({'name': a.name, 'tooltip': a.desc or entry.get('tooltip', ''),
                          'group': 'My add-ins', 'panel': a.group or a.owner or '',
                          'kind': 'action'})
            out[a.id] = entry
            add_group('My add-ins')
    except Exception:
        pass

    # "My add-ins" first (the ones he actually maintains), Fusion's workspaces in
    # their own order after, then the catch-alls at the bottom.
    tail = ['Application', 'Other commands', 'Not loaded']
    ordered = ([g for g in groups if g == 'My add-ins']
               + [g for g in groups if g != 'My add-ins' and g not in tail]
               + [g for g in tail if g in groups])
    return list(out.values()), ordered


# ── Recents ────────────────────────────────────────────────────────────────────

def _load_recents():
    global _recents
    try:
        with open(RECENTS_F) as f:
            data = json.load(f)
        if isinstance(data, list):
            _recents = [r for r in data if isinstance(r, dict) and r.get('id')][:MAX_RECENTS]
    except (OSError, ValueError):
        _recents = []


def _save_recents():
    try:
        os.makedirs(EDITOR_DIR, exist_ok=True)
        with open(RECENTS_F, 'w') as f:
            json.dump(_recents, f, indent=1)
    except OSError:
        pass


def _note_command(cid):
    """Record a command the user just ran. Called on Fusion's main thread from
    the commandStarting event, so it must stay cheap — no file I/O here; the sync
    tick flushes to disk."""
    global _recents, _recents_dirty, _last_recent_id
    if not cid or cid in RECENT_SKIP or cid == _last_recent_id:
        return
    _last_recent_id = cid
    _recents = [r for r in _recents if r.get('id') != cid]
    _recents.insert(0, {'id': cid, 'ts': int(time.time() * 1000)})
    del _recents[MAX_RECENTS:]
    _recents_dirty = True


# ── Writing a binding ──────────────────────────────────────────────────────────

def _text(cmd):
    """Run a Fusion text command, returning its output ('' on failure)."""
    try:
        return app.executeTextCommand(cmd) or ''
    except Exception:
        return ''


def _open_shortcut_dialog(cmd_id):
    """Hand off to Fusion's own "Change Keyboard Shortcut" prompt for one command.

    Blocks Fusion's main thread until the prompt is dismissed — it's modal — which
    also stalls this sync handler. That's fine: the HTTP server runs on its own
    thread, so the window stays responsive, and the next tick picks up whatever
    binding Fusion applied. The prompt doesn't display the command's name, but it
    is bound to the id passed here (see the module docstring)."""
    if not cmd_id:
        return False, 'No command'
    _text('HotKey.Dialog %s' % cmd_id)
    return True, 'Set in Fusion’s dialog'


def _push_live_keys():
    """Mirror Fusion's in-memory keymap to the server, so the window shows the real
    current state rather than whatever was last flushed to the options file."""
    raw = _text('HotKey.List')
    if not raw:
        return
    try:
        doc = json.loads(raw)
    except ValueError:
        return
    try:
        # Apply: On restart — queued changes are replayed on top, so the re-adoption
        # below agrees with the queue instead of undoing it (a queued Clear would
        # otherwise be re-adopted from the still-live anchor within a second).
        # See fusion_ui/keymap.py's docstring.
        entries = _keymap().overlay(_keymap().entries(doc))
        _load_server().update_live_keys(entries)
        # Fusion's own keyboard panel stays a legitimate front door: a key pointed at
        # one of our actions there IS the assignment, and it also identifies which
        # claimant is the anchor we get to intercept.
        _hotkeys().sync_from_keymap(entries)
    except Exception:
        pass


def _keymap():
    """The shared keymap module (server.py parses the options file with it too)."""
    global _keymap_mod
    if _keymap_mod is None:
        spec = importlib.util.spec_from_file_location(
            'fusion_shortcuts_keymap', os.path.join(os.path.dirname(HOTKEYS_PY), 'keymap.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _keymap_mod = mod
    return _keymap_mod


# ── Background poller ──────────────────────────────────────────────────────────

# Set when an event has been fired and not yet handled, so ticks cannot stack up
# behind a busy main thread and then drain together. Holds the monotonic time it
# was fired; the 5 s staleness reset is a backstop so a notify lost to an add-in
# reload cannot stop the poller for good.
_tick_pending = 0.0


def _poller():
    global _tick_pending
    while not _stop_evt.is_set():
        now = time.monotonic()
        if not _tick_pending or now - _tick_pending > 5.0:
            _tick_pending = now
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                _tick_pending = 0.0
        _stop_evt.wait(0.4)


# ── Main-thread sync ───────────────────────────────────────────────────────────

def _sync():
    global _catalog_built, _recents_dirty, _last_keys_push
    mod = _server_mod
    if mod is None:
        return

    for cmd in mod.pop_commands():
        action = cmd.get('action')
        try:
            if action in ('assign', 'dialog'):
                ok, msg = _open_shortcut_dialog(cmd.get('cmd_id', ''))
                mod.set_result(ok, msg)
                _push_live_keys()          # reflect it without waiting for the timer

            elif action == 'command':
                cd = ui.commandDefinitions.itemById(cmd.get('cmd_id', ''))
                if cd:
                    cd.execute()

            elif action == 'bind':
                when = cmd['when'] if 'when' in cmd else None
                typing = cmd['typing'] if 'typing' in cmd else None
                _hotkeys().set_binding(cmd.get('cmd_id', ''), cmd.get('seq', ''), when, typing)
                _push_registry()
                mod.set_result(True, 'Bound')

            elif action == 'route':
                _hotkeys().ensure_route(cmd.get('seq', ''))
                _push_registry()
                mod.set_result(True, 'Routed')

            elif action == 'press':
                _hotkeys().press(cmd.get('seq', ''), any_key=bool(cmd.get('any')))

            elif action == 'rebuild':
                _catalog_built = False

            elif action == 'probe':
                # HotKey.List alone is ~40 KB, so keep the cap well above it.
                mod.set_debug(probe=_text(cmd.get('text', ''))[:200000])

        except Exception:
            mod.set_debug(last_error=traceback.format_exc())
            mod.set_result(False, 'Failed: see /debug')

    # The re-route fallback: _execute_or_defer queues here instead of calling
    # CommandDefinition.execute() straight from commandStarting, for whichever of
    # the two native re-route paths turns out to need it (see hotkeys.py). Same
    # execute() as the 'command' action above, just off the event handler's stack.
    try:
        deferred, _hotkeys()._deferred[:] = list(_hotkeys()._deferred), []
        for cid in deferred:
            cd = ui.commandDefinitions.itemById(cid)
            if cd:
                cd.execute()
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())

    if not _catalog_built:
        try:
            commands, groups = _build_catalog()
            _catalog_kinds.clear()
            _catalog_kinds.update({c['id']: c.get('kind') for c in commands})
            mod.update_catalog(commands, groups)
            _catalog_built = True
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())

    if _recents_dirty:
        _recents_dirty = False
        mod.update_recents(list(_recents))
        _save_recents()

    # Fusion's live keymap. Cheap enough to refresh on a timer, and it's the only
    # view that reflects a shortcut changed in Fusion's own panel straight away.
    now = time.time()
    if now - _last_keys_push >= KEYS_PUSH_EVERY:
        _last_keys_push = now
        # First and on its own: remapd reads the command/sketch stamps as stale 3s
        # after the last refresh, so a push below raising must not skip it.
        try:
            _hotkeys().refresh_command()
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())
        try:
            _push_live_keys()
            _push_registry()
            _apply_labels()
            # The catalog is built once; an add-in reloaded since declares actions it
            # never saw, which search then could not find (Go to Source, 2026-10-08).
            if _catalog_built and any(a.id not in _catalog_kinds for a in _hotkeys().actions()):
                _catalog_built = False
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())


# ── Renames, applied to Fusion itself ─────────────────────────────────────────
#
# Control Room renames a command by writing a label into remap/fusion_view.json.
# For OUR commands — add-in buttons and registry actions — that label is put on
# Fusion's CommandDefinition too, so Fusion's own keyboard panel and toolbar say
# the same thing. Runtime only, no add-in source is touched, and removing the
# label restores the original. Re-applied every tick because an add-in re-asserts
# its own name when it starts (Extrude does), and this must win afterwards.
# Fusion's own commands are never renamed: that is Fusion's UI, not ours.
_catalog_kinds = {}       # id -> kind, from the last catalog build
_label_originals = {}     # id -> the name it had before we renamed it

def _apply_labels():
    global _catalog_built
    mod = _server_mod
    if mod is None or not hasattr(mod, 'labels_now'):
        return
    labels = mod.labels_now()
    defs = ui.commandDefinitions
    # Put back anything whose label has gone.
    for cid in list(_label_originals):
        if cid not in labels:
            cd = defs.itemById(cid)
            try:
                if cd and cd.name != _label_originals[cid]:
                    cd.name = _label_originals[cid]
            except Exception:
                pass
            _label_originals.pop(cid, None)
    for cid, label in labels.items():
        if _catalog_kinds.get(cid) not in ('addin', 'action'):
            continue
        cd = defs.itemById(cid)
        if not cd:
            continue
        try:
            if cd.name == label:
                continue
            if cid not in _label_originals:
                _label_originals[cid] = cd.name
            cd.name = label
            _catalog_built = False        # the window's catalog carries the name too
        except Exception:
            pass


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def run(context):
    global _custom_evt, _active
    try:
        _load_recents()
        _start_server()

        # The context router. Installing it here rather than in each add-in keeps one
        # commandStarting handler for the whole system; every other add-in only has to
        # declare its actions. Idempotent across reloads by design.
        try:
            hk = _hotkeys()
            hk.declare(
                SEARCH_CMD_ID, 'Command Search',
                'Every command, ours and Fusion’s, by any word in its name — not '
                'only how it starts. Assign it a key here (S is the natural pick — '
                'see fusion_shortcuts/CLAUDE.md) to replace Fusion’s own search.',
                owner='Shortcuts', run=_open_search)
            hk.declare(
                JUMP_CMD_ID, 'Jump to Name',
                'Type or dictate a component or Browser folder name; Enter goes '
                'there, as the Browser would (Scripts/jump/).',
                owner='Shortcuts', run=_open_jump)
            hk.declare(
                GOTO_CMD_ID, 'Go To',
                'Say a component, Browser folder or body name (Wispr Flow); it goes '
                'there — a body is picked where it sits (Scripts/jump/goto.sh).',
                owner='Shortcuts', run=_open_goto)
            hk.attach()
        except Exception:
            pass
        if _server_mod:
            _server_mod.update_recents(list(_recents))

        _active = True
        _stop_evt.clear()
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = SyncHandler()
        _custom_evt.add(h)
        handlers.append(h)

        # Our own "recently used commands" feed.
        cs = CommandStartingHandler()
        ui.commandStarting.add(cs)
        handlers.append(cs)

        threading.Thread(target=_poller, daemon=True).start()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        hc = CommandCreatedHandler()
        cmd_def.commandCreated.add(hc)
        handlers.append(hc)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Fusion Shortcuts failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _custom_evt, _active
    try:
        _active = False
        _stop_evt.set()
        _save_recents()
        handlers.clear()
        _stop_server()
        if _custom_evt:
            try:
                app.unregisterCustomEvent(EVENT_ID)
            except Exception:
                pass
            _custom_evt = None
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
    except Exception:
        pass


# ── Event handlers ─────────────────────────────────────────────────────────────

class SyncHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        # Module-level flag rather than `self in handlers`: Fusion's Python binding
        # may rebuild the wrapper object on each notify, so identity is unreliable.
        global _tick_pending
        _tick_pending = 0.0
        if not _active:
            return
        # Stand down if anything else is already in the model. This tick walks
        # commandDefinitions, which the 09-22 spindump caught on the stack at the
        # moment of a crash. See Tick Guard.py.
        with tickguard.tick('shortcuts') as go:
            if not go:
                return
            mod = _server_mod
            if mod:
                mod.bump_notify()
            try:
                _sync()
            except Exception:
                if mod:
                    mod.set_debug(last_error=traceback.format_exc())


class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if not _active:
            return
        try:
            _note_command(args.commandId)
        except Exception:
            pass


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = CommandExecuteHandler()
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        """Toggle the window: running when we're pressed means close it."""
        try:
            running = subprocess.run(
                ['pgrep', '-x', APP_PROC],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if running:
                subprocess.Popen(['pkill', '-x', APP_PROC])
            else:
                subprocess.Popen(['open', '-a', APP_PATH])
        except Exception:
            ui.messageBox(
                'Could not toggle FusionShortcuts.app.\nBuild it first:\n'
                '  /Users/josh/Code/Scripts/fusion_shortcuts/FusionShortcutsApp/build.sh')
