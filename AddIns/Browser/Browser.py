"""
Fusion Browser — add-in.

Runs a local HTTP server (server.py) and, every ~350 ms via a custom event (so
all Fusion API calls land on the main thread), pushes a hierarchy snapshot to it
and applies any actions the browser window posted (activate / toggle visibility /
rename / forward-a-command / edit sheet-metal rule).

Architecture mirrors the Fusion Params add-in. A toolbar button toggles the
FusionBrowser.app window open/closed.
"""

import adsk.core, adsk.fusion
import builtins, contextlib, importlib.util, json, os, re, subprocess, sys, threading, time, traceback
import types as _types

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

CMD_ID   = 'FusionBrowserCmd'
CMD_NAME = 'Fusion Browser'
CMD_DESC = 'Open the custom component browser'
PANEL_ID = 'SolidScriptsAddinsPanel'
EVENT_ID = 'FusionBrowserSyncEvent'

# Viewport-side commands that mirror the browser window's Cmd+R / Cmd+E, so the same
# actions work while Fusion's viewport (not the window) is focused. They act on the
# current viewport selection, falling back to the active component. These are
# "dual-homed": both surfaces implement the same intent natively, so one binding in
# Fusion's keymap covers both and nothing is forwarded.
# "Go in" and "Go in deep" — either can be on F. Both are handed to the Dimension Value
# add-in when a sketch dimension is selected, and to Rectangular Pattern when a sketch
# array's marker is; see the guard in HotkeyStartingHandler.
GOIN_CMD_IDS     = ('BrowserGoInCmd', 'BrowserGoInDeepCmd')

# Commands that move the viewport to a DIFFERENT LEVEL outright, as opposed to
# stepping in or out of where you already are. The window is told when one starts so
# its spine tidy runs even mid-sketch-exit — see note_nav_jump() in server.py for why
# it cannot work that out for itself.
NAV_JUMP_CMD_IDS = frozenset({'UnisolateTopLevelCmd', 'GoToBottomLevelCmd'})

# Fusion's own Cut and Paste. They keep those chords — this add-in only takes one when
# the browser window is holding something the key can ONLY mean for it (parameters
# selected, or a cut waiting to be placed), so ⌘X / ⌘V work from the viewport without
# taking anything away from Fusion. Interception rather than a keymap entry, for the
# same reason the Dimension Value add-in claims "go in": there is nothing to bind, and
# a binding would have to win the chord outright.
CUT_CMD_ID       = 'CutCommand'
PASTE_CMD_ID     = 'PasteCommand'
# The window-side ids they are handed over as. Deliberately NOT CommandDefinitions:
# nothing ever binds these, they only ever arrive by the hand-over above.
BROWSER_CUT_ID   = 'BrowserCutCmd'
BROWSER_PASTE_ID = 'BrowserPasteCmd'

RENAME_CMD_ID    = 'BrowserRenameSelectionCmd'
TOGGLEVIS_CMD_ID = 'BrowserToggleVisSelectionCmd'
ENTERSKETCH_CMD_ID = 'BrowserEnterSketchSelectionCmd'
# One browser or the other, never both: the dock and Fusion's own Browser swap places.
SWAP_CMD_ID      = 'BrowserSwapNativeCmd'
# Back into the sketch edited before this one (or the last one, if none is open).
RECENT_SKETCH_CMD_ID = 'JumpToRecentSketchCmd'
# Esc while something is running, else Deselect — see _escape_or_deselect.
ESCDESEL_CMD_ID  = 'EscapeOrDeselectCmd'
# Everything open, at once: our windows' dialogs and Fusion's command — see _super_escape.
SUPERESC_CMD_ID  = 'SuperEscapeCmd'

# Window-homed actions that also mean something in the VIEWPORT when the dock is not
# there to receive them. Without this table, F and D pressed with the dock closed were
# vetoed and dropped — the veto only forwards while the window is alive, and the ids
# have no native behaviour of their own. The twin is the viewport command that says the
# same thing in Fusion's terms (see fusion_ui/actions.json, the 'dual' entries).
_WINDOW_ACTION_TWINS = {
    'BrowserGoInCmd':  'ActivateAndIsolateCmd',
    'BrowserGoInDeepCmd': 'ActivateAndIsolateCmd',
    'BrowserGoOutCmd': 'GoToParentComponentCmd',
}

EDITOR_DIR = '/Users/josh/Code/Scripts/fusion_browser'
SERVER_PY  = os.path.join(EDITOR_DIR, 'server.py')
# server.py re-imports this fresh on every load; its shared-asset list (the /tl_*.js
# scripts…) is part of what the server answers, so an edit to it restarts the server too.
WEBWIN_PY  = os.path.join(os.path.dirname(EDITOR_DIR), 'fusion_ui', 'webwin.py')


def _server_stamp():
    return max(os.path.getmtime(SERVER_PY), os.path.getmtime(WEBWIN_PY))
# This add-in's toolbar button / hotkey now toggles the COMBINED dock window
# (FusionDock.app = Browser on top + Parameters on bottom), not the old standalone
# Browser window. The server above (port 8732) still feeds the Browser pane.
APP_PATH   = '/Applications/FusionDock.app'
APP_PROC   = 'FusionDock'

# Actions whose BEHAVIOUR lives in the browser window's JS, declared once in
# fusion_ui/actions.json and read from there by both sides. Each gets a real
# CommandDefinition below, which is what makes it bindable in Fusion's own keymap and
# visible in the Shortcuts window — so there is one keymap, not two. Pressed with the
# viewport focused, the command is vetoed before it can run and the id is handed to the
# window instead; pressed with the window focused, the page recognises the id in the
# keymap it already fetches and runs the same JS directly, with no round trip.
_browser_action_ids = set()

# Shared visibility engine (loaded fresh from disk, same as the nav add-ins).
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load_local('fvs', 'Visibility Snapshot.py')
# The process-wide "someone is already in the model" latch — read its header before
# changing anything about this tick. It is why the sync below can decline to run.
tickguard = _load_local('tickguard', 'Tick Guard.py')
# Per-component "hide edges when this component isn't active" policy.
edgepol = _load_local('edgepol', 'Edge Policy.py')
edgepol.timeline_pos = tickguard.timeline_pos
# Where the last physical click landed (canvas vs timeline / panel).
clickreg = _load_local('clickreg', 'Click Region.py')
# The one whole-item delete Browser, Timeline and Delete Selection share.
deleting = _load_local('deleting', 'Deleting.py')
# Where a body a command just made came from, so the window can file it beside it.
newbodies = _load_local('newbodies', 'New Bodies.py')
newbodies.leaf_key_for = fvs.leaf_key_for
newbodies.leaf_by_key = fvs.leaf_by_key
newbodies.keyed = fvs._keyed
newbodies.timeline_pos = tickguard.timeline_pos
# The palette's Default for New Bodies — shared with Extrude, which paints its preview.
defapp = _load_local('defapp', 'Default Appearance.py')
try:
    # Only for reading the line a sketch's plane was placed by, so the row can offer to
    # open that line again. Never a reason for the browser not to start.
    bnds = _load_local('bnds', 'Bounds.py')
except Exception:
    bnds = None

# Flagged component ids for the current document. Cached — only this add-in writes
# the file, so it's reloaded when the document changes or right after we toggle.
_edge_flags = set()
_edge_hidden = set()
_edge_when = {}         # comp id -> frozenset(trigger comp ids) — Edge Policy's hideEdgesWhen
_edge_doc_key = None


def _doc_key():
    """Stable per-document key: the dataFile id, or a name-based one when unsaved."""
    try:
        doc = app.activeDocument
        if not doc:
            return ''
        try:
            df = doc.dataFile
            if df and df.id:
                return df.id
        except Exception:
            pass
        return '__unsaved__:' + (doc.name or '')
    except Exception:
        return ''


def _refresh_edge_flags(force=False):
    global _edge_flags, _edge_hidden, _edge_when, _edge_doc_key
    key = _doc_key()
    if force or key != _edge_doc_key:
        _edge_doc_key = key
        _edge_flags = edgepol.flagged_ids(key)
        _edge_hidden = edgepol.hidden_ids(key)
        _edge_when = edgepol.edge_triggers(key)
        edgepol.invalidate(key)     # new document -> re-apply from scratch
    return key


def _edit_sketch_owner(design):
    """Component that owns the sketch currently open for edit, or None.

    Fed to the edge policy so a flagged component shows its edges while you are in
    one of its own sketches. Entering a sketch does not necessarily activate its
    owner — a timeline double-click leaves the active component where it was — so
    without this the bodies you are sketching against are the ones drawn edgeless.

    fvs owns the identification: design.activeEditObject raises for a sketch nested
    in an occurrence, so it falls back to the sketch remembered at SketchActivate.
    When a sketch is open but can't be identified, this returns None and the policy
    behaves exactly as it did before.
    """
    try:
        editing, native, _tok = fvs._edit_sketch_info(design)
        if editing and native is not None:
            return native.parentComponent
    except Exception:
        pass
    return None


def _last_click_ts():
    """Epoch-ms stamp of the last left-click over Fusion, from the event tap.

    Pushed to the browser window so it can dismiss its context menu when you click
    in the viewport — the window can't see those clicks itself.
    """
    try:
        with open(_CLICK_CMD_FILE) as f:
            return int(f.read().split()[0])
    except Exception:
        return 0


_click_published = None


def _publish_click():
    """Hand the window each new click stamp with where it landed (canvas or chrome),
    every tick. Only the TREE push is gated on a change signature, and a click on
    empty canvas changes nothing in it, so the window would otherwise never hear of
    it — and it needs to: that click is what drops a selection the viewport can't
    hold (parameters, groups). The widget read is memoised per click in Click Region
    and happens once here per new stamp. Main thread (called from the sync tick)."""
    global _click_published
    ms = _last_click_ts()
    if ms == _click_published:
        return
    try:
        mod = _server_mod
        if mod is None or not hasattr(mod, 'patch_click'):
            return
        mod.patch_click(ms, clickreg.click_over_canvas())
        _click_published = ms
    except Exception:
        pass


def _publish_extrude():
    """While our Extrude is open: say so, and which bodies it works on (as body row
    keys), every tick. The page then turns a body or group click into "add/remove from
    the extrude" (extrudebodies) and marks the bodies that take part."""
    hook = sys.modules.get('fusion_extrude_hook')
    got = {'on': False, 'keys': []}
    if hook is not None and hasattr(hook, 'state'):
        try:
            got = hook.state()
        except Exception:
            pass
    try:
        mod = _server_mod
        if mod is not None and hasattr(mod, 'patch_extrude'):
            mod.patch_extrude(got.get('on'), got.get('keys'))
    except Exception:
        pass


def _publish_peek(design):
    """Suspend mode's peek, as the row token the window roots on (ROOT_TOKEN for the
    root, None for no peek), every tick — a peek made from a viewport key changes no
    tree, so it can't wait for a rebuild. Returned for the change signature."""
    tok = None
    try:
        has, occ = fvs._peek_target(design)
        if has:
            tok = ROOT_TOKEN if occ is None else _occ_token(occ)
    except Exception:
        pass
    try:
        mod = _server_mod
        if mod is not None and hasattr(mod, 'patch_peek'):
            mod.patch_peek(tok)
    except Exception:
        pass
    return tok

_httpd        = None
_server_mod   = None
_server_mtime = 0.0
_stop_evt     = threading.Event()
_custom_evt   = None
_active       = False

# Lightweight change detection so the sync can skip the costly full tree rebuild
# (which serializes an entityToken for every occurrence/body/sketch) when nothing the
# browser shows has changed. What the signature can't see — visibility, renames,
# bodies added — is picked up by rebuilding on the first tick after any command ends
# (_build_due, set by CommandTerminatedHandler and by this window's own commands), and
# by a forced rebuild every _FULL_BUILD_MAX seconds for changes made with no command
# at all (another add-in's API writes): those can take up to that long to show. The
# forced one waits for a pause in your input (_FORCED_BUILD_IDLE_S): it is ~50 ms
# normally but 300 ms+ when Fusion is in a slow patch, and landing in the middle of a
# click or a drag is exactly the hitch you feel (2026-10-07).
_last_sig        = None
_last_full_build = 0.0
_FULL_BUILD_MAX  = 6.0
_FORCED_BUILD_IDLE_S = 1.0
_build_due       = True
_cg_idle_fn      = None


def _input_idle():
    """Seconds since the last real keyboard/mouse/trackpad input (HID state; Flush
    Undo's rule). No fork. A large number if CoreGraphics can't be reached."""
    global _cg_idle_fn
    try:
        if _cg_idle_fn is None:
            import ctypes
            cg = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            f = cg.CGEventSourceSecondsSinceLastEventType
            f.restype = ctypes.c_double
            f.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            _cg_idle_fn = f
        return _cg_idle_fn(1, 0xFFFFFFFF)   # HID system state, any input type
    except Exception:
        return 1e9
# Commands whose end can't change anything the tree shows. SelectCommand "ends" each
# time another command starts; that one's own end is what counts.
_NO_TREE_CMDS = frozenset({
    'SelectCommand', 'ConstrainedOrbitCommand', 'FreeOrbitCommand', 'PanCommand',
    'ZoomCommand', 'SetOrbitCenterCommand', 'ResetOrbitCenterCommand', 'FitCommand',
})

# entityToken costs ~1 ms per call on Apple Silicon (measured; ~0.4 ms on Intel), and the
# tree build needs one for every occurrence, sketch and body — together the largest single
# cost in _build_tree. So tokens are memoised, and the whole cache is dropped whenever the
# document or the timeline moves.
#
# That stamp is NOT enough on its own: a Delete that removes a sketch and adds a Remove
# feature for a body, a Combine edit that consumes or frees bodies, or a reorder all keep
# (count, marker) the same while shifting which sketch/body sits at each index. A cached
# token then belonged to a NEIGHBOUR — every row from that index on acted on (selected,
# hid, RENAMED) the wrong entity, and the same token turning up under a new name looked
# like a rename, so folder filings and hidden records were carried onto the wrong row
# (2026-09-29: 34 Ribs rows off by one or two). Every hit is therefore checked against
# the entity it was read from with == (a native identity compare, ~0.001 ms — ~90x
# cheaper than the token), and re-read on a mismatch.
_tok_cache = {}      # key -> (entity the token was read from, token)
_tok_stamp = None


def _tok_cached(key, ent):
    hit = _tok_cache.get(key)
    if hit is not None:
        try:
            if hit[0] == ent:
                return hit[1]
        except Exception:
            pass                     # the old entity is gone — read afresh
    tok = ent.entityToken
    _tok_cache[key] = (ent, tok)
    return tok


def _tok_reset_if_stale(design):
    """Drop the token cache when the model may have changed under it."""
    global _tok_cache, _tok_stamp
    try:
        tl = design.timeline
        stamp = (app.activeDocument.name, tl.count, tl.markerPosition)
    except Exception:
        stamp = None                 # can't tell -> don't trust the cache at all
    if stamp is None or stamp != _tok_stamp:
        _tok_cache = {}
        _tok_stamp = stamp


def _occ_token(occ):
    """entityToken for an occurrence, memoised on its much cheaper fullPathName."""
    try:
        key = ('o', occ.fullPathName)
    except Exception:
        return occ.entityToken
    return _tok_cached(key, occ)


def _leaf_token(comp_id, kind, idx, ent):
    """entityToken for a sketch/body, memoised on (component id, kind, index) and
    checked against the entity itself — an index can hold a different sketch/body with
    the stamp unchanged (see _tok_cache)."""
    if not comp_id:
        return ent.entityToken
    return _tok_cached((kind, comp_id, idx), ent)

# Tokens of a selection the Timeline add-in's Trace command made (dock['quiet_select']):
# features plus their sketches. While the viewport still holds exactly that (same row
# tokens, same count) the page is told which FEATURES it is (state.selFeats) and picks
# their feature rows instead of the sketch rows — without opening anything to them
# (state.selQuiet).
_quiet_sel = None
_quiet_feats = []
_quiet_n = 0

# /tmp file the FusionScrollFilter event tap stamps on every left-click over Fusion:
# "<epoch_ms> <0|1>" (1 = Cmd was held). Lets the viewport honor "plain click enters a
# sketch, Cmd-click just selects" — the modifier the selection poll can't otherwise see.
_CLICK_CMD_FILE = '/tmp/fusion_click_cmd'

# The event tap swallows a right-click (a two-finger tap — Click Actions' gesture) only
# while this says Fusion is idle, so a running command's menu still gets it.
_SELECTMODE_FILE = '/tmp/fusion_selectmode'


_selectmode_last = (None, 0.0)   # (last flag written, ts of that write)
# The reader (FusionScrollFilter.fusionIsIdle) treats the stamp as stale after 1500ms,
# so a refresh well inside that window is all the freshness it needs.
_SELECTMODE_REFRESH = 0.5


def _write_selectmode(idle):
    """Tell the event tap whether Fusion is idle (activeCommand == SelectCommand), so it
    only swallows right-clicks for the Go-Up gesture while idle.

    Written on every change, and otherwise refreshed at _SELECTMODE_REFRESH — writing it
    on every sync instead meant a /tmp write at the full sync rate for a value that
    changes rarely, and /tmp is real disk on macOS."""
    global _selectmode_last
    flag = 1 if idle else 0
    now = time.time()
    last_flag, last_ts = _selectmode_last
    if flag == last_flag and (now - last_ts) < _SELECTMODE_REFRESH:
        return
    try:
        with open(_SELECTMODE_FILE, 'w') as f:
            f.write('{} {}'.format(int(now * 1000), flag))
        _selectmode_last = (flag, now)
    except Exception:
        pass


# Fusion with NO active command — activeCommand '' instead of SelectCommand. A panel
# add-in that ends its command with doExecute(True) (Extrude, Mirror, Feature Panel:
# their palettes are hidden, so nothing else reaches the command) leaves Fusion here,
# and it stays until some command runs. In it, every selection write from inside a
# command throws "InternalValidationError : command": Space's Trace / Edit (Timeline's
# _select) failed silently until the next unrelated command. Reproduced 2026-09-27:
# Smart Extrude edit → cancelextrude → activeCommand '' → Space → sel.clear() throws.
# SelectCommand.execute() puts Fusion back and keeps the selection.
_NO_CMD_GRACE = 0.4      # s — '' between two commands is a blip, not this state
_no_cmd_since = None
_no_cmd_fixed = 0.0


def _restore_select(active):
    global _no_cmd_since, _no_cmd_fixed
    if active:
        _no_cmd_since = None
        return
    now = time.time()
    if _no_cmd_since is None:
        _no_cmd_since = now
        return
    if now - _no_cmd_since < _NO_CMD_GRACE or now - _no_cmd_fixed < 2.0:
        return
    _no_cmd_fixed = now
    cd = ui.commandDefinitions.itemById('SelectCommand')
    if cd:
        cd.execute()


def _selection_has_dimension(design):
    """True when the viewport selection includes a sketch dimension (a go-in key with
    one selected belongs to the Dimension Value dialog, not to us) — or a sketch
    array's marker, which the Rectangular Pattern add-in edits on go-in."""
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            try:
                e = sel.item(i).entity
                if (isinstance(e, adsk.fusion.SketchDimension)
                        or adsk.fusion.RectangularPatternConstraint.cast(e) is not None):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


# ── Server helpers ───────────────────────────────────────────────────────────
def _load_server():
    global _server_mod, _server_mtime
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('fusion_browser_server', SERVER_PY)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _server_mod = mod
        if hasattr(mod, 'set_wake'):
            mod.set_wake(_wake_tick)
        try:
            _server_mtime = _server_stamp()
        except OSError:
            _server_mtime = 0.0
    return _server_mod


def _start_server():
    global _httpd
    mod = _load_server()
    if _httpd is None:
        try:
            _httpd = mod.make_server()
            threading.Thread(target=_httpd.serve_forever, daemon=True).start()
        except OSError:
            _httpd = None  # port taken — another instance already serving


def _stop_server():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None


def _check_server_fresh():
    """server.py — and fusion_ui/webwin.py, which it re-imports fresh on every
    load — lives outside the AddIns folder, reached by absolute path (SERVER_PY),
    the same way fusion_ui/hotkeys.py is. Reload Recent AddIns only re-execs files
    INSIDE the AddIns folder whose own mtime changed, so an edit to either of
    those two is invisible to it: this add-in's Browser.py never changes, so it is
    never picked to reload, and the module loaded at the top of this Fusion
    session — routes and all — answers forever otherwise.

    Called from the background poller (below), not the 70 ms model-sync tick:
    tearing the socket down and rebuilding it is pure Python, no adsk.* object
    touched, so it is safe off the main thread, and this is a once-per-few-seconds
    stat() rather than something that belongs on that tick's budget."""
    global _server_mod
    if _server_mod is None:
        return
    try:
        stamp = _server_stamp()
    except OSError:
        return
    if stamp != _server_mtime:
        _stop_server()
        _server_mod = None
        _start_server()


# Sync cadence. This was 0.15 when a tick was expensive; _sync now runs at a ~2 ms
# median (see the entityToken cache above), so a 70 ms period costs ~3% of one core and
# takes ~40 ms off the average click-to-update latency.
_SYNC_PERIOD = 0.07

# Set when an event has been fired and not yet handled, so ticks cannot stack up
# behind a busy main thread and then drain as one long beachball — at 70 ms a
# three-second command used to leave ~40 of them queued. Holds the monotonic time
# it was fired; the 5 s staleness reset is a backstop so a notify lost to an add-in
# reload cannot stop the poller for good. (Same shape as Parameters' poller.)
_tick_pending = 0.0

# How often the poller stat()s server.py for _check_server_fresh — once every few
# seconds is plenty for an edit-then-reload workflow and costs nothing worth
# throttling further.
_SERVER_CHECK_PERIOD = 2.0
_server_checked_at = 0.0


def _wake_tick():
    """Called on the server's thread the moment the window posts a command, so it
    runs now rather than on the next poll. Same no-stacking rule as the poller: a
    tick already queued will pop the command itself."""
    global _tick_pending
    if not _active or _tick_pending:
        return
    _tick_pending = time.monotonic()
    try:
        app.fireCustomEvent(EVENT_ID, '')
    except Exception:
        _tick_pending = 0.0


def _poller():
    global _tick_pending, _server_checked_at
    while not _stop_evt.is_set():
        now = time.monotonic()
        if not _tick_pending or now - _tick_pending > 5.0:
            _tick_pending = now
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                _tick_pending = 0.0
        if now - _server_checked_at > _SERVER_CHECK_PERIOD:
            _server_checked_at = now
            try:
                _check_server_fresh()
            except Exception:
                pass
        _stop_evt.wait(_SYNC_PERIOD)


# ── Display-name cleanup ─────────────────────────────────────────────────────
def _display_name(name):
    """Strip trailing dedup / instance numbers for display only.
    'Leg - Veneer (1)' -> 'Leg - Veneer', 'Fuselage:1' -> 'Fuselage',
    'Body 2' -> 'Body'.  The real Fusion name is never changed."""
    if not name:
        return name
    s = re.sub(r'\s*\(\d+\)\s*$', '', name)   # " (1)"
    s = re.sub(r':\d+$', '', s)                # ":1"
    s = re.sub(r'\s+\d+$', '', s)              # trailing " 2"
    return s.strip() or name



# ── Session performance trace ────────────────────────────────────────────────
# "Fusion is snappy after a restart, then degrades" is an in-process effect: it survives
# on a machine with 32 GB and zero swap, and a Fusion restart resets it. Point-in-time
# benchmarks can't separate that from background load (Time Machine, Spotlight), so
# instead record the REAL tree-build cost in situ and flush a summary once a minute.
# A genuine degradation shows as a rising min/median across the session; noise doesn't.
_PERF_LOG      = os.path.expanduser('~/fusion_build_perf.csv')
_PERF_FLUSH_S  = 60.0
_perf_samples  = []
_perf_start    = time.time()
_perf_last     = 0.0


def _perf_record(ms):
    """Cheap: append a float, and once a minute write one summary line."""
    global _perf_samples, _perf_last
    _perf_samples.append(ms)
    now = time.time()
    if _perf_last == 0.0:
        _perf_last = now
        return
    if now - _perf_last < _PERF_FLUSH_S or not _perf_samples:
        return
    xs = sorted(_perf_samples)
    try:
        fresh = not os.path.exists(_PERF_LOG)
        with open(_PERF_LOG, 'a') as f:
            if fresh:
                f.write('time,session_min,builds,min_ms,median_ms,max_ms,'
                        'tok_cache,notify\n')
            srv = _server_mod
            notify = (getattr(srv, '_debug', {}) or {}).get('notify_count', -1) if srv else -1
            f.write('%s,%d,%d,%.2f,%.2f,%.2f,%d,%s\n' % (
                time.strftime('%H:%M:%S'), int((now - _perf_start) / 60.0), len(xs),
                xs[0], xs[len(xs) // 2], xs[-1], len(_tok_cache), notify))
    except Exception:
        pass
    _perf_samples = []
    _perf_last = now


# ── Hierarchy snapshot ───────────────────────────────────────────────────────
def _occ_key(occ, comp_id, parent_key):
    """Durable identity for an occurrence: the chain of component ids from the top,
    each with its instance number ("oc|<cid>:1/<cid>:2").

    The same idea as fvs._leaf_key, for the same reason: the entityToken is a fast
    within-session handle that rots between sessions, and the window persists its row
    overlays (custom order, collapse state) under this instead.  Component ids and
    instance numbers survive renames, saves and reopens — fullPathName, which is the
    same chain written in names, does not survive a rename."""
    try:
        name = occ.name
    except Exception:
        name = ''
    inst = name.rsplit(':', 1)[-1] if ':' in name else '1'
    seg = '{}:{}'.format(comp_id or name, inst)
    return (parent_key + '/' + seg) if parent_key else ('oc|' + seg)


def _node_for_occurrence(occ, active_token, bodies_on, sketches_on, parent_key='',
                         parent_path=''):
    comp = occ.component
    # "empty" = truly nothing inside, so the UI can show an "(empty)" placeholder.
    try:
        empty = (occ.childOccurrences.count == 0 and comp.bRepBodies.count == 0
                 and comp.sketches.count == 0)
    except Exception:
        empty = False
    try:
        comp_id = comp.id
    except Exception:
        comp_id = ''
    # One lookup, reused for the row, the active test and the children below: this used
    # to be three separate entityToken calls per occurrence.
    occ_token = _occ_token(occ)
    occ_key = _occ_key(occ, comp_id, parent_key)
    # The isolate ledger's path (fvs._occ_path_str), built on the way down rather than
    # walked back up from every occurrence.
    try:
        occ_path = (parent_path + '/' + occ.name) if parent_path else occ.name
    except Exception:
        occ_path = parent_path
    bulb = bool(occ.isLightBulbOn)
    node = {
        'type': 'component',
        'name': _display_name(comp.name),
        'token': occ_token,
        # Same contract as the leaf rows below: `key` is the durable identity the
        # window persists row state under, `token` the within-session handle.
        'key': occ_key,
        'visible': bulb,
        # userHidden = the user's INTENT for this bulb, as for sketches and bodies. A
        # bulb isolation switched off (or on) is read from the isolate ledger — the value
        # it had before navigation touched it — so a jump never makes a component look
        # hidden or shown by hand. The window strikes rows from this, not from `visible`.
        'userHidden': not _vis_occ_led.get(occ_path, bulb),
        'active': occ_token == active_token,
        'sheetmetal': _has_sheetmetal(comp),
        'empty': empty,
        'occToken': '',
        'groupOff': False,
        'compId': comp_id,
        'hideEdges': comp_id in _edge_flags,
        'hideBody': comp_id in _edge_hidden,
        'hideEdgesWhen': sorted(_edge_when.get(comp_id, ())),
        'children': _children_for_component(occ, comp, active_token, bodies_on,
                                            sketches_on, occ_token, occ_key, occ_path),
    }
    return node


# token -> the durable key it was last drawn under. A leaf key carries the NAME, so a
# rename — from our window or from Fusion's — shows up here as the same token with a
# new key, and the hidden record (which is keyed the same way) is carried across. The
# window does the same for its folder membership; see rebuildKeyIndex in index.js.
_key_by_leaf_token = {}


def _design_now():
    try:
        return adsk.fusion.Design.cast(app.activeProduct)
    except Exception:
        return None


def _note_leaf_keys(design, pairs):
    """[(token, key)] for one component's sketches or bodies. Carries the moved ones
    together: a repeated name's '#n' shifts when the first of two is renamed or
    deleted, so one leaf's old key can be another's new one (fvs.carry_leaf_keys)."""
    live = {k for _t, k in pairs}
    moves = []
    for tok, key in pairs:
        was = _key_by_leaf_token.get(tok)
        if was and was != key:
            moves.append((was, key))
    # An old key still drawn by a leaf that did NOT move is not this leaf's to carry.
    arriving = {n for _o, n in moves}
    moves = [(o, n) for o, n in moves if o not in live or o in arriving]
    if moves:
        fvs.carry_leaf_keys(design, moves)
        was_hidden = [(n, o in _vis_hidden) for o, n in moves]
        for o, _n in moves:
            _vis_hidden.discard(o)
        for n, hid in was_hidden:
            if hid:
                _vis_hidden.add(n)
            else:
                _vis_hidden.discard(n)
    for tok, key in pairs:
        _key_by_leaf_token[tok] = key


def _keyed_rows(design, comp_id, kind, coll):
    """(entity, key, token) for each sketch/body, in order, keyed by fvs._keyed (the
    one place a repeated name's '#n', or a body's stamped id, is decided). The index
    is only the token cache's slot; the cache checks the entity, so a skipped item
    costs a token read, never a wrong token."""
    rows = [(e, k, _leaf_token(comp_id, kind, i, e))
            for i, (k, e) in enumerate(fvs._keyed(comp_id, kind, coll))]
    _note_leaf_keys(design, [(t, k) for _e, k, t in rows])
    return rows


def _stamp_bodies(design, comp_ids):
    """The window filed a body that has no id yet (fvs.stamp_bodies): stamp every body
    in its component, then carry the hidden records from the name keys to the id keys
    at once — before Hide or anything else can write under the new key and have the
    next tick's carry overwrite it — and the folder filings in folders.json
    (server.carry_leaf_folders), since the window only carries its own copy (index.js
    rebuildKeyIndex: same token, new key) while it is polling. Inside the
    command's one_step; never in a sketch edit, where an undo step is unsafe — the
    filing keeps its name key and the next gesture outside the sketch stamps it."""
    if tickguard._in_sketch():
        return
    for cid in comp_ids or ():
        try:
            comp = design.allComponents.itemById(cid) if cid else design.rootComponent
        except Exception:
            comp = None
        if comp is None:
            continue
        before = {t: k for _e, k, t in _keyed_rows(design, cid, 'bd', comp.bRepBodies)}
        if not fvs.stamp_bodies(comp):
            continue
        after = _keyed_rows(design, cid, 'bd', comp.bRepBodies)   # carries the hidden
        moves = [(before[t], k) for _e, k, t in after if before.get(t, k) != k]
        try:
            _server_mod.carry_leaf_folders(fvs._lineage(design), moves)
        except Exception:
            _server_mod.set_debug(last_error='stamp carry: ' + traceback.format_exc())


def _leaf_nodes(comp, occ_token, bodies_on, sketches_on):
    """Sketch + body rows for a component — ALWAYS listed (sketches above bodies);
    when the body/sketch global toggle is off they carry groupOff so the UI greys
    them out instead of dropping them.  occToken lets the UI build the in-context
    proxy for selection (a native body token isn't selectable in the viewport)."""
    out = []
    try:
        comp_id = comp.id
    except Exception:
        comp_id = ''
    design = _design_now()

    def keyed(kind, coll):
        return _keyed_rows(design, comp_id, kind, coll)

    for s, key, tok in keyed('sk', comp.sketches):
        out.append({
            'type': 'sketch', 'name': _display_name(s.name),
            'token': tok,
            # key      = durable identity (component, kind, name), what the window
            #            persists group membership under.  The token is a fast
            #            within-session handle only; it is what used to rot.
            # visible  = EFFECTIVE (folder, ancestors, isolate all fold into it) —
            #            for greying only.
            # own      = this item's own bulb — the only thing a hide/show writes.
            # userHidden = the recorded intent, not a guess from `visible`.
            'key': key, 'own': bool(s.isLightBulbOn),
            'userHidden': key in _vis_hidden,
            'occToken': occ_token, 'visible': bool(s.isVisible), 'groupOff': not sketches_on,
            'active': False, 'sheetmetal': False, 'empty': False, 'children': [],
            # The line the plane box placed this sketch by, or '' for every sketch made
            # any other way. Read on every tick rather than cached: it changes when the
            # line is edited, and an attribute lookup across the whole document measures
            # at ~1 ms — an eighth of what reading isVisible for the same rows costs.
            'datum': bnds.sketch_datum_text(s) if bnds is not None else '',
        })
    bodies = []
    for b, key, tok in keyed('bd', comp.bRepBodies):
        bodies.append({
            'type': 'body', 'name': _display_name(b.name),
            'token': tok,
            'key': key, 'own': bool(b.isLightBulbOn),
            'userHidden': key in _vis_hidden,
            'occToken': occ_token, 'visible': bool(b.isVisible), 'groupOff': not bodies_on,
            'active': False, 'sheetmetal': False, 'empty': False, 'children': [],
        })
    # Every body sits under the Geometry group — the window's name for Fusion's Bodies
    # folder. Emitted even with no bodies (the window drops an empty one, and draws it
    # again the moment a body or a subgroup gives it something to hold).
    out.append(_geometry_group_node(comp, comp_id, occ_token, bodies))
    # The Geometry group — the component's timeline features, only for a component with
    # bodies of its own AND features: the rows are the features of those bodies (a mirror
    # or derive container has features but no bodies, and gets no group). Its rows come
    # from the Timeline add-in, grafted in by the window.
    try:
        has_feats = bool(bodies) and comp.features.count > 0
    except Exception:
        has_feats = False
    if has_feats:
        out.append(_features_group_node(comp, comp_id, occ_token))
    # The Origin group — only while the window's Origins switch is on (_setvis has the
    # rest of the story: reading seven bulbs a component is a cost worth paying only
    # when someone actually asked to see them), and only for a component with sketches
    # of its OWN (comp.sketches is never the nested components'): the origin planes are
    # what those sketches sit on, and a component without any has no use for the row.
    if _server_mod is not None and comp.sketches.count:
        try:
            show_origins = bool(_server_mod.ui_flags().get('showOrigins'))
        except Exception:
            show_origins = False
        if show_origins:
            out.append(_origin_group_node(comp, comp_id, occ_token))
    # The Construction group — the component's own construction planes, while the
    # window's Construction Planes switch is on (off, Object Visibility hides every
    # plane in the viewport and the rows go with them — _sync_construction_vis).
    if _server_mod is not None and _show_construction() and comp.constructionPlanes.count:
        out.append(_construction_group_node(comp, comp_id, occ_token))
    return out


# The Geometry group row's token is synthetic ('bg:<comp id>'), like the Origin
# group's below: Fusion's Bodies folder has no entity of its own.
GEOMETRY_GROUP_PREFIX = 'bg:'


def _geometry_group_node(comp, comp_id, occ_token, bodies):
    """The 'Geometry' group row — the window's reflection of the component's Bodies
    folder — with the bodies as its children. `own` is the folder bulb; the row is
    drawn greyed (groupOff) rather than struck through when that bulb is off, since
    the bulb is the body toggle's doing, not a hide of anything (see _set_bodies_folder).
    The window grafts its own subgroups into this row; see graftFolders in index.js."""
    try:
        folder_on = bool(comp.isBodiesFolderLightBulbOn)
    except Exception:
        folder_on = True
    return {
        'type': 'geometry', 'name': 'Bodies', 'token': GEOMETRY_GROUP_PREFIX + comp_id,
        'key': GEOMETRY_GROUP_PREFIX + comp_id, 'own': folder_on, 'userHidden': False,
        'occToken': occ_token, 'visible': True, 'groupOff': not folder_on,
        'compId': comp_id,
        'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
        'children': bodies,
    }


# The (timeline) Geometry group row's token is synthetic too ('fx:<comp id>').
FEATURES_GROUP_PREFIX = 'fx:'


def _features_group_node(comp, comp_id, occ_token):
    """The 'Geometry' group row — the component's timeline features. Empty here: the
    rows are the Timeline add-in's (/feats, watched while the group is open), grafted
    in by the window (graftFeatures in index.js). No bulb of its own — 'groupOff'
    mirrors the Bodies-folder bulb (same reading _geometry_group_node does) purely so
    the row greys in step with Bodies when Cycle Visibility (AddIns/Visibility
    Snapshot.py) turns that folder off for the active component: the features drawn
    here belong to those very bodies, so there is nothing to act on in the viewport
    either while they're off."""
    try:
        folder_on = bool(comp.isBodiesFolderLightBulbOn)
    except Exception:
        folder_on = True
    return {
        'type': 'features', 'name': 'Operations', 'token': FEATURES_GROUP_PREFIX + comp_id,
        'key': FEATURES_GROUP_PREFIX + comp_id, 'own': True, 'userHidden': False,
        'occToken': occ_token, 'visible': True, 'groupOff': not folder_on,
        'compId': comp_id,
        'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
        'children': [],
    }


# The Origin group row's token is synthetic ('og:<comp id>') — there's no single
# Fusion entity for "the Origin folder" to hang a real entityToken off of.
ORIGIN_GROUP_PREFIX = 'og:'

# name, the getter off a Component — every entity Fusion keeps in a component's Origin
# folder. Order is the display order: planes, then axes, then the point.
_ORIGIN_ENTS = (
    ('XY', lambda c: c.xYConstructionPlane),
    ('XZ', lambda c: c.xZConstructionPlane),
    ('YZ', lambda c: c.yZConstructionPlane),
    ('X',  lambda c: c.xConstructionAxis),
    ('Y',  lambda c: c.yConstructionAxis),
    ('Z',  lambda c: c.zConstructionAxis),
    ('Origin Point', lambda c: c.originConstructionPoint),
)


def _origin_group_node(comp, comp_id, occ_token):
    """The 'Origin' group row and its seven children.

    The group row itself is a browser-only grouping (like a virtual folder) — always
    drawn ungreyed ('visible': True) — while each child reflects the REAL Fusion
    entity: 'own' is its own bulb, 'visible' folds in the folder bulb too, since an
    entity with its own bulb on is still invisible while the Origin folder is off.
    """
    try:
        folder_on = bool(comp.isOriginFolderLightBulbOn)
    except Exception:
        folder_on = False
    kids = []
    for name, getter in _ORIGIN_ENTS:
        try:
            ent = getter(comp)
            tok = ent.entityToken
        except Exception:
            continue
        try:
            own = bool(ent.isLightBulbOn)
        except Exception:
            own = False
        kids.append({
            'type': 'originent', 'name': name, 'token': tok,
            'key': fvs._leaf_key(comp_id, 'og', name),
            'own': own, 'userHidden': not own,
            'occToken': occ_token, 'visible': own and folder_on, 'groupOff': False,
            'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
            'children': [],
        })
    return {
        'type': 'origin', 'name': 'Origin', 'token': ORIGIN_GROUP_PREFIX + comp_id,
        'key': ORIGIN_GROUP_PREFIX + comp_id, 'own': folder_on, 'userHidden': False,
        'occToken': occ_token, 'visible': True, 'groupOff': False,
        'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
        'children': kids,
    }


# The Construction group row: synthetic like Origin's ('cg:<comp id>'). It holds the
# component's construction PLANES; its bulb is Fusion's Construction folder bulb, which
# also covers the component's construction axes and points (not listed).
CONSTRUCTION_GROUP_PREFIX = 'cg:'


def _tree_flags():
    """The window switches that change which rows the add-in builds — part of the
    rebuild signature, so flipping one redraws now rather than at the safety rebuild."""
    try:
        f = _server_mod.ui_flags()
        return (bool(f.get('showOrigins')), f.get('showConstruction'))
    except Exception:
        return None


def _show_construction():
    """The window's Construction Planes switch. Unknown until the window has said (None
    in the server) reads as on — Fusion's own default, planes shown."""
    try:
        return _server_mod.ui_flags().get('showConstruction') is not False
    except Exception:
        return True


def _construction_group_node(comp, comp_id, occ_token):
    """The 'Construction' group row and one child per construction plane — each child a
    real Fusion entity, 'own' its bulb and 'visible' folding in the folder bulb, exactly
    as _origin_group_node does for the Origin folder."""
    try:
        folder_on = bool(comp.isConstructionFolderLightBulbOn)
    except Exception:
        folder_on = True
    kids = []
    for pl in comp.constructionPlanes:
        try:
            tok = pl.entityToken
            name = pl.name
        except Exception:
            continue
        try:
            own = bool(pl.isLightBulbOn)
        except Exception:
            own = False
        kids.append({
            'type': 'conplane', 'name': _display_name(name), 'token': tok,
            'key': fvs._leaf_key(comp_id, 'cg', name),
            'own': own, 'userHidden': not own,
            'occToken': occ_token, 'visible': own and folder_on, 'groupOff': False,
            'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
            'children': [],
        })
    return {
        'type': 'construction', 'name': 'Construction',
        'token': CONSTRUCTION_GROUP_PREFIX + comp_id,
        'key': CONSTRUCTION_GROUP_PREFIX + comp_id, 'own': folder_on, 'userHidden': False,
        'occToken': occ_token, 'visible': True, 'groupOff': False,
        'fixed': True, 'active': False, 'sheetmetal': False, 'empty': False,
        'children': kids,
    }


def _children_for_component(occ, comp, active_token, bodies_on, sketches_on,
                            occ_token=None, occ_key='', occ_path=''):
    kids = []
    for child in occ.childOccurrences:
        kids.append(_node_for_occurrence(child, active_token, bodies_on, sketches_on,
                                         occ_key, occ_path))
    if occ_token is None:
        occ_token = _occ_token(occ)
    kids.extend(_leaf_nodes(comp, occ_token, bodies_on, sketches_on))
    return kids


def _has_sheetmetal(comp):
    try:
        return comp.activeSheetMetalRule is not None
    except Exception:
        return False


# Sentinel token used by the UI to activate the document root (no Top Level node).
ROOT_TOKEN = '__root__'


# Intent + group membership for the tree currently being built.  Read once per
# build (not per row): a row must be drawn as user-hidden from the RECORD, never
# from a dark bulb — a bulb is dark for four different reasons and the window used
# to attribute all of them to the user.
_vis_hidden = set()
_vis_groups = {}
_vis_occ_led = {}     # isolate ledger: occurrence path -> its bulb before isolation


def _build_tree(design, bodies_on, sketches_on):
    global _vis_hidden, _vis_groups, _vis_occ_led
    _tok_reset_if_stale(design)
    try:
        _vis_hidden, _vis_occ_led = fvs.load_intent(design)
        _vis_groups = fvs.hidden_group_members(fvs._lineage(design))
    except Exception:
        _vis_hidden, _vis_groups, _vis_occ_led = set(), {}, {}
    root = design.rootComponent
    # Active occurrence token; '' when the root component itself is active (no
    # node is bold then — the document root isn't shown as a row).
    active_occ = fvs.recall_active_occ(design)
    if not (active_occ and active_occ.component == design.activeComponent):
        active_occ = fvs.find_occurrence(design, design.activeComponent)
    active_token = active_occ.entityToken if active_occ else ''

    tree = []
    for occ in root.occurrences:
        tree.append(_node_for_occurrence(occ, active_token, bodies_on, sketches_on))
    # Bodies / sketches directly on the root component show at the top level.
    tree.extend(_leaf_nodes(root, '', bodies_on, sketches_on))
    return tree, active_token


# ── Action execution (main thread) ───────────────────────────────────────────
def _find_entity(design, token):
    try:
        ents = design.findEntityByToken(token)
        return ents[0] if ents else None
    except Exception:
        return None


def _occ_of(entity):
    if isinstance(entity, adsk.fusion.Occurrence):
        return entity
    if hasattr(entity, 'assemblyContext') and entity.assemblyContext:
        return entity.assemblyContext
    return None


def _activate_token(design, token):
    if token == ROOT_TOKEN:
        target = None          # activate the document root
    else:
        ent = _find_entity(design, token)
        if not ent:
            return
        target = _occ_of(ent)
    _activate_target(design, target)


def _peek_token(design, token):
    """Suspend mode's navigation: look at the row's occurrence (ROOT_TOKEN → the root)
    without activating it — fvs § Suspend mode: peek."""
    if token == ROOT_TOKEN:
        target = None
    else:
        ent = _find_entity(design, token)
        if not ent:
            return
        target = _occ_of(ent)
    if not fvs.is_suspended():
        # The add-in was reloaded under a suspended window (run() clears the flag):
        # the window is still the authority on the mode.
        fvs.set_suspended(design, True)
        _glow(True)
    fvs.peek(design, target)


# ── Suspend glow ─────────────────────────────────────────────────────────────
# While suspend mode is on, a soft blue glow fades in round Fusion's window
# (bin/fusion_glow.m — follows the window, hides while Fusion isn't in front, exits
# when Fusion does). Same shell-out pattern as Hide Sketch Palette's sketch_palette.
# Off by name, not by handle: a reload loses _glow_proc, and an orphan must still go.
GLOW_BIN = '/Users/josh/bin/fusion_glow'
_glow_proc = None


def _glow(on):
    global _glow_proc
    try:
        if on:
            if _glow_proc is not None and _glow_proc.poll() is None:
                return
            subprocess.run(['/usr/bin/pkill', '-x', 'fusion_glow'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _glow_proc = subprocess.Popen([GLOW_BIN, str(os.getpid())],
                                          stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          start_new_session=True)
        else:
            _glow_proc = None
            subprocess.run(['/usr/bin/pkill', '-x', 'fusion_glow'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _tlog('glow error:', traceback.format_exc().replace('\n', ' | '))


def _activate_target(design, target):
    """_activate_target_inner as one undo step — already so from the Browser tick,
    this covers the callers outside it."""
    with tickguard.one_step('Navigate'):
        return _activate_target_inner(design, target)


def _activate_target_inner(design, target):
    """Activate `target` (an Occurrence) or the document root (target is None),
    handling the isolate un-isolate/re-isolate dance. Shared by token activation
    and the viewport jump-down."""
    # If we're exiting a sketch edit by activating a component, remember the edited
    # sketch's parent component. Activating a component implicitly finishes the sketch
    # but does NOT run Go Up's SketchStop cleanup, so the sibling sketches hidden on
    # entry would stay hidden. After the activation we re-show them (set_sketches_shown)
    # when we've landed in that parent — same result as exiting via Go Up.
    #
    # Returns True when the activation was NOT done because a master opened by Edit
    # Original is being left instead — back to the sketch it came from (see fvs's
    # master-session section). Callers that go on to do more must stop.
    #
    # While the dock shows the vertical timeline the active component is LOCKED
    # (AddIns/Timeline: the list is that component's subtree), so any move elsewhere —
    # the viewport jump-down included — is refused here rather than undone a tick later.
    dock = getattr(builtins, '_fusion_dock', None)
    if (isinstance(dock, dict) and dock.get('lockOcc') is not None and dock.get('live')
            and dock.get('lockDoc') == _doc_key()):     # a lock is per document
        try:
            want = target.fullPathName if target else ''
        except Exception:
            want = None
        if want != dock['lockOcc']:
            return False
    was_editing = False
    edit_parent_id = None
    try:
        if fvs.is_editing_sketch(design) and fvs.request_master_return(design):
            return True
    except Exception:
        pass
    try:
        if fvs.is_editing_sketch(design):
            was_editing = True
            _sk = fvs.recall_edit_sketch(design) or fvs.edited_sketch_from_marker(design)
            if _sk:
                pc = (_sk.nativeObject or _sk).parentComponent
                edit_parent_id = pc.id if pc else None
    except Exception:
        pass

    cur = fvs.recall_active_occ(design)
    if not (cur and cur.component == design.activeComponent):
        cur = fvs.find_occurrence(design, design.activeComponent)

    # A real activation made while suspended (the window's escape hatch) supersedes
    # any peek. The ledger undoes the peek's isolation exactly, so un-isolating from
    # `cur` below still lands clean.
    fvs.clear_peek()

    if fvs.isolate_is_active():
        # Un-isolate everything from root down to where we were, then re-isolate
        # onto the new target — handles activating anywhere in the tree.
        fvs.take_snapshot(design, cur)
        # Bulbs from the ledger (exact undo of isolation); folder state still from the
        # snapshot, which is the only place it is recorded.
        path = []
        c = cur
        while c is not None:
            path.append(c)
            try:
                c = c.assemblyContext
            except Exception:
                break
        path.reverse()
        # Buffered (fvs § Bulb batch): the un-isolate switches siblings back on and
        # the re-isolate switches most of them off again — only the net change is
        # written, when the block ends.
        with fvs.bulb_batch():
            if fvs.unisolate_from_ledger(design):
                fvs.restore_folders_only(design, None)
                for occ in path:
                    fvs.restore_folders_only(design, occ)
            else:
                fvs.restore_snapshot(design, None)
                for occ in path:
                    fvs.restore_snapshot(design, occ)
            if target:
                fvs.isolate_occurrence_no_snapshot(design, target)

    if target:
        target.activate()
        fvs.remember_activation(target)
    else:
        design.activateRootComponent()
        fvs.remember_activation(None)
    fvs.apply_policy(design)

    # Exited a sketch into its parent component → the sibling sketches (hidden on
    # entry) take the arrival default (bodies only where there are bodies), matching
    # Go Up's behaviour.
    if was_editing and edit_parent_id is not None:
        try:
            if design.activeComponent.id == edit_parent_id:
                fvs.exit_sketch_restore_sketches(design)
        except Exception:
            pass


# ── Origin folder ownership ───────────────────────────────────────────────────
# The seven Origin-folder entities (_ORIGIN_ENTS / _origin_group_node) are ordinary
# ConstructionPlane/Axis/Point entities, so their OWN bulbs go through the generic
# _setvis/_togglevis below like any other row. What's special about them is the
# FOLDER bulb: Fusion shows every entity under a closed folder once it's opened, so
# turning one on has to manage the folder (and, the first time, blank the siblings)
# rather than just flip its own bulb — handled here, and threaded into _setvis /
# _togglevis for any token that resolves to one of these seven.

def _is_origin_entity(ent):
    """True when `ent` is one of a component's built-in Origin-folder entities — not
    a construction plane/axis/point the user or the plane box made."""
    try:
        comp = ent.component
    except Exception:
        return False
    if comp is None:
        return False
    try:
        candidates = (comp.xYConstructionPlane, comp.xZConstructionPlane,
                      comp.yZConstructionPlane, comp.xConstructionAxis,
                      comp.yConstructionAxis, comp.zConstructionAxis,
                      comp.originConstructionPoint)
    except Exception:
        return False
    for c in candidates:
        try:
            if c is not None and c == ent:
                return True
        except Exception:
            pass
    return False


def _origin_siblings(comp):
    """Every Origin-folder entity of `comp`, in display order."""
    out = []
    for _name, getter in _ORIGIN_ENTS:
        try:
            ent = getter(comp)
        except Exception:
            ent = None
        if ent is not None:
            out.append(ent)
    return out


def _component_by_id(design, comp_id):
    if not comp_id:
        return None
    try:
        if design.rootComponent.id == comp_id:
            return design.rootComponent
        for c in design.allComponents:
            if c.id == comp_id:
                return c
    except Exception:
        pass
    return None


def _mark_origins_shown(comp, shown):
    """Persist which components have an Origin entity the user wants visible, so
    apply_policy / restore_origin_default (Visibility Snapshot.py) know to leave that
    component's Origin folder open instead of forcing it back off on the next
    navigation tick."""
    if fvs is None:
        return
    try:
        comp_id = comp.id
    except Exception:
        return
    state = fvs._read_shared_state()
    ids = set(state.get('origins_shown') or [])
    if shown:
        ids.add(comp_id)
    else:
        ids.discard(comp_id)
    state['origins_shown'] = sorted(ids)
    fvs._write_shared_state(state)


def _set_origin_entity_vis(ent, visible):
    """Turn one Origin-folder entity on or off, managing the folder bulb around it."""
    comp = ent.component
    visible = bool(visible)
    try:
        folder_was_on = bool(comp.isOriginFolderLightBulbOn)
    except Exception:
        folder_was_on = False
    if visible and not folder_was_on:
        # Everything is nominally ON under a closed folder — opening it now would
        # reveal all seven unless the others are blanked first.
        for sib in _origin_siblings(comp):
            try:
                sib.isLightBulbOn = False
            except Exception:
                pass
    try:
        ent.isLightBulbOn = visible
    except Exception:
        pass
    if visible:
        try:
            comp.isOriginFolderLightBulbOn = True
        except Exception:
            pass
        _mark_origins_shown(comp, True)
    else:
        still_on = any(bool(getattr(sib, 'isLightBulbOn', False))
                       for sib in _origin_siblings(comp))
        if not still_on:
            try:
                comp.isOriginFolderLightBulbOn = False
            except Exception:
                pass
            _mark_origins_shown(comp, False)


def _toggle_origin_group(design, group_token):
    """Bulk on/off for the whole Origin group — the group row's own click. The
    component is found by id (`og:<comp id>`) since the group has no entity of its
    own to resolve a token against."""
    comp = _component_by_id(design, group_token[len(ORIGIN_GROUP_PREFIX):])
    if comp is None:
        return
    try:
        turn_on = not bool(comp.isOriginFolderLightBulbOn)
    except Exception:
        turn_on = True
    for ent in _origin_siblings(comp):
        try:
            ent.isLightBulbOn = turn_on
        except Exception:
            pass
    try:
        comp.isOriginFolderLightBulbOn = turn_on
    except Exception:
        pass
    _mark_origins_shown(comp, turn_on)


# ── Construction folder (the window's Construction group) ────────────────────
# Same shape as the Origin folder above: a plane's own bulb does nothing while the
# component's Construction folder is off, so showing one opens the folder — blanking
# its siblings first the first time, or opening it would reveal them all too.

def _construction_siblings(comp):
    """Everything the Construction folder bulb governs: planes, axes, points."""
    out = []
    for coll in ('constructionPlanes', 'constructionAxes', 'constructionPoints'):
        try:
            out.extend(list(getattr(comp, coll)))
        except Exception:
            pass
    return out


def _is_construction_plane(ent):
    return isinstance(ent, adsk.fusion.ConstructionPlane) and not _is_origin_entity(ent)


def _set_construction_plane_vis(ent, visible):
    comp = ent.component
    visible = bool(visible)
    try:
        folder_was_on = bool(comp.isConstructionFolderLightBulbOn)
    except Exception:
        folder_was_on = True
    if visible and not folder_was_on:
        for sib in _construction_siblings(comp):
            try:
                sib.isLightBulbOn = False
            except Exception:
                pass
    try:
        ent.isLightBulbOn = visible
    except Exception:
        pass
    if visible and not folder_was_on:
        try:
            comp.isConstructionFolderLightBulbOn = True
        except Exception:
            pass


def _set_construction_group(design, group_token, visible=None):
    """The group row's bulb: every plane of the component and the folder together.
    `visible` None flips it from the folder bulb."""
    comp = _component_by_id(design, group_token[len(CONSTRUCTION_GROUP_PREFIX):])
    if comp is None:
        return
    if visible is None:
        try:
            visible = not bool(comp.isConstructionFolderLightBulbOn)
        except Exception:
            visible = True
    for pl in comp.constructionPlanes:
        try:
            pl.isLightBulbOn = bool(visible)
        except Exception:
            pass
    try:
        comp.isConstructionFolderLightBulbOn = bool(visible)
    except Exception:
        pass


# The switch itself: off, Fusion's Object Visibility ▸ Construction Planes goes off —
# every construction plane in the design leaves the viewport while each keeps its own
# bulb, so turning the switch back on brings back exactly what was showing. Written
# only when the switch CHANGES (not every tick), so flipping Object Visibility by hand
# in Fusion isn't fought.
_construction_vis_applied = None


def _sync_construction_vis():
    global _construction_vis_applied
    try:
        want = _server_mod.ui_flags().get('showConstruction')
    except Exception:
        return
    if want is None or want == _construction_vis_applied:
        return
    try:
        cdef = adsk.core.ListControlDefinition.cast(
            ui.commandDefinitions.itemById('VisibilityOverrideCommand').controlDefinition)
        for i in range(cdef.listItems.count):
            it = cdef.listItems.item(i)
            if it.name.strip() == 'Construction Planes':   # list items can be indented
                if it.isSelected != bool(want):
                    it.isSelected = bool(want)
                break
    except Exception:
        _server_mod.set_debug(last_error='construction vis: ' + traceback.format_exc())
    _construction_vis_applied = want


# ── Bodies folder (the window's Geometry group) ──────────────────────────────
def _in_active_subtree(design, comp):
    """Is `comp` one the body toggle acts on — the active component, anything nested
    under it, everything under its parent while the body toggle falls back a level, or
    everything while isolation is switched off?"""
    if fvs is None:
        return False
    try:
        if fvs.isolate_off_active():
            return True
        active = design.activeComponent
        if comp == active or comp.id in fvs._descendant_ids(active):
            return True
        if fvs.fallback_active('body'):
            parent = fvs.isolate_parent_occ(fvs._active_occ(design))
            scope = parent.component if parent else design.rootComponent
            return comp == scope or comp.id in fvs._descendant_ids(scope)
        return False
    except Exception:
        return False


def _set_bodies_folder(design, comp_id, visible):
    """Show/hide a component's Bodies folder — the Geometry group row's own click.

    Inside the active subtree the folder bulb IS the body toggle's transient flag
    (apply_policy rewrites it from that flag on every pass), so the toggle is what has
    to flip — a direct write there would be undone on the next navigation. Anywhere
    else the bulb is written directly, exactly as clicking it in Fusion's own browser
    would; the policy re-asserts its default on the next navigation, there too."""
    comp = _component_by_id(design, comp_id)
    if comp is None:
        return
    try:
        cur = bool(comp.isBodiesFolderLightBulbOn)
    except Exception:
        return
    if cur == bool(visible):
        return
    if _in_active_subtree(design, comp):
        try:
            fvs.toggle_bodies(design)
        except Exception:
            pass
        return
    try:
        comp.isBodiesFolderLightBulbOn = bool(visible)
    except Exception:
        pass


def _setvis(design, tokens, visible):
    """Set these entities' own bulbs to an ABSOLUTE value, as a DELIBERATE hide/show.

    Absolute, because the window used to send "toggle" and compute the flip from
    `isVisible` — the EFFECTIVE state, which is off whenever the folder is off, an
    ancestor is hidden or isolate is on.  Setting isVisible writes the OWN bulb but
    reading it returns the effective one, so a row that looked hidden for a reason
    of its own could be toggled the wrong way, or not move at all.  The window now
    decides the value and this just applies it.

    Everything through here is the user's own doing, and is recorded as intent.
    There is deliberately no "policy" variant: a group, a toggle or isolation never
    writes a leaf bulb from here — it changes its own state and asks for a policy
    pass, which is the single place leaf bulbs are computed.  A second write path
    that touched bulbs without recording anything is exactly what the old group
    override was.
    """
    keys = []
    for token in tokens:
        if token.startswith(GEOMETRY_GROUP_PREFIX):
            _set_bodies_folder(design, token[len(GEOMETRY_GROUP_PREFIX):], visible)
            continue
        if token.startswith(ORIGIN_GROUP_PREFIX):
            comp = _component_by_id(design, token[len(ORIGIN_GROUP_PREFIX):])
            if comp is not None:
                for ent in _origin_siblings(comp):
                    try:
                        ent.isLightBulbOn = bool(visible)
                    except Exception:
                        pass
                try:
                    comp.isOriginFolderLightBulbOn = bool(visible)
                except Exception:
                    pass
                _mark_origins_shown(comp, bool(visible))
            continue
        if token.startswith(CONSTRUCTION_GROUP_PREFIX):
            _set_construction_group(design, token, bool(visible))
            continue
        ent = _find_entity(design, token)
        if not ent:
            continue
        if _is_origin_entity(ent):
            _set_origin_entity_vis(ent, visible)
            continue
        if _is_construction_plane(ent):
            _set_construction_plane_vis(ent, visible)
            continue
        try:
            if isinstance(ent, adsk.fusion.Occurrence):
                ent.isLightBulbOn = bool(visible)
                fvs.note_occ_intent(ent, bool(visible))
                continue
            if hasattr(ent, 'isLightBulbOn'):
                ent.isLightBulbOn = bool(visible)
            elif hasattr(ent, 'isVisible'):
                ent.isVisible = bool(visible)
        except Exception:
            continue
        k = fvs.leaf_key_for(ent)
        if k:
            keys.append(k)
    if keys:
        fvs.set_leaf_intent(design, keys, not visible)


def _togglevis(design, tokens):
    """Flip each entity from its OWN bulb (never from the effective state)."""
    for token in tokens:
        if token.startswith(GEOMETRY_GROUP_PREFIX):
            comp = _component_by_id(design, token[len(GEOMETRY_GROUP_PREFIX):])
            try:
                cur = bool(comp.isBodiesFolderLightBulbOn)
            except Exception:
                continue
            _set_bodies_folder(design, token[len(GEOMETRY_GROUP_PREFIX):], not cur)
            continue
        if token.startswith(ORIGIN_GROUP_PREFIX):
            _toggle_origin_group(design, token)
            continue
        if token.startswith(CONSTRUCTION_GROUP_PREFIX):
            _set_construction_group(design, token)
            continue
        ent = _find_entity(design, token)
        if not ent:
            continue
        if _is_origin_entity(ent):
            try:
                cur = bool(ent.isLightBulbOn) and bool(ent.component.isOriginFolderLightBulbOn)
            except Exception:
                cur = False
            _set_origin_entity_vis(ent, not cur)
            continue
        if _is_construction_plane(ent):
            try:
                cur = (bool(ent.isLightBulbOn)
                       and bool(ent.component.isConstructionFolderLightBulbOn))
            except Exception:
                cur = False
            _set_construction_plane_vis(ent, not cur)
            continue
        try:
            cur = bool(ent.isLightBulbOn)
        except Exception:
            continue
        _setvis(design, [token], not cur)


def _rename(design, token, new_name):
    ent = _find_entity(design, token)
    if not ent or not new_name:
        return
    try:
        if isinstance(ent, adsk.fusion.Occurrence):
            _rename_component(design, ent.component, new_name)
        else:
            ent.name = new_name
    except Exception:
        pass


def _rename_component(design, comp, new_name):
    """Rename a component AND its bounds parameters, which carry its name by convention
    as their owner level (<Owner>_Bounds_… — see Bounds.py's Names). The window
    files parameters by name, so each rename is pushed to it as the same edit the value
    dialog sends, and the filing follows."""
    old_name = comp.name
    comp.name = new_name
    if bnds is None or old_name == comp.name:
        return
    try:
        renamed, problems = bnds.rename_parameters(design, comp, old_name)
    except Exception:
        _tlog('rename bounds params:', traceback.format_exc().replace('\n', ' | '))
        return
    if _server_mod:
        for was, now in renamed:
            _server_mod.push_ui_action('BrowserParamEditedCmd', {'name': was, 'newName': now})
    if problems:
        _tlog('rename bounds params:', '; '.join(problems))


def _disregard(design, token, mode):
    """Tag a component's name so the browser stops drawing it (see fvs.is_disregarded).

    The tag lives in the NAME on purpose: it is visible in Fusion's own browser and
    undone by editing the name there, with no hidden state anywhere. Done add-in side
    rather than in the window because the window shows names with their trailing
    dedup/instance numbers stripped — rebuilding a name from that would drop the "(2)"
    off "Veneer (2)" and rename the wrong thing.

    mode 'all' hides the component AND everything inside it; anything else hides only
    the component itself, and its contents move up a level.
    """
    ent = _find_entity(design, token)
    if not ent:
        return
    try:
        comp = ent.component if isinstance(ent, adsk.fusion.Occurrence) else ent
        cur = comp.name or ''
        if fvs.DISREGARD_TAG in cur.lower():
            return                       # already tagged — leave the existing wording
        comp.name = cur + (' (disregard all)' if mode == 'all' else ' (disregard)')
    except Exception:
        pass


def _select_body(design, token, occ_token):
    ent = _find_entity(design, token)
    if not ent:
        return
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(ent)
        return
    except Exception:
        pass
    # Native body token isn't selectable — build the in-context proxy.
    try:
        occ = _find_entity(design, occ_token) if occ_token else None
        proxy = ent.createForAssemblyContext(occ) if occ else ent
        sel.add(proxy)
    except Exception:
        pass


def _in_context(design, token, occ_token):
    """The entity behind a row, as the in-context proxy when it sits under an
    occurrence. None if the token no longer resolves."""
    ent = _find_entity(design, token)
    if not ent:
        return None
    occ = _find_entity(design, occ_token) if occ_token else None
    try:
        if (occ and not isinstance(ent, adsk.fusion.Occurrence)
                and hasattr(ent, 'createForAssemblyContext')):
            return ent.createForAssemblyContext(occ)
    except Exception:
        pass
    return ent


def _extrude_pick(design, items, toggle):
    """While Extrude is open, a sketch clicked here means ALL of its profiles — never the
    sketch itself (nothing in an extrude wants one). True if Extrude took the click, and
    then nothing else may touch the viewport selection: it holds the extrude's profiles.

    items: [(token, occToken)]."""
    hook = sys.modules.get('fusion_extrude_hook')
    if hook is None:
        return False
    sketches = []
    for token, occ_token in items:
        ent = _in_context(design, token, occ_token)
        if ent is not None and adsk.fusion.Sketch.cast(ent) is not None:
            sketches.append(ent)
    try:
        if not hook.pick_sketches(sketches, toggle):
            return False       # no extrude running
    except Exception:
        return False
    return True


def _extrude_bodies(design, items, group=''):
    """Body rows clicked while Extrude is open — one body, or every body of a clicked
    group (`group`, its folder id) — go in or out of what it works on, as one unit.
    Never selected."""
    hook = sys.modules.get('fusion_extrude_hook')
    if hook is None or not hasattr(hook, 'pick_bodies'):
        return
    bodies = []
    for it in items:
        ent = _in_context(design, it.get('token', ''), it.get('occToken', ''))
        if ent is not None and adsk.fusion.BRepBody.cast(ent) is not None:
            bodies.append(ent)
    if bodies:
        try:
            hook.pick_bodies(bodies, group or None)
        except TypeError:                       # an Extrude from before groups
            hook.pick_bodies(bodies)


# Above this many curves a sketch is selected as the Sketch alone (profiles only).
# Each curve is its own Selections.add and the cost grows with the selection
# (~0.45 ms + 0.006 ms per item already selected, 260612 2026-10-06; Selections.all =
# a collection is no faster): 150 curves ≈ 140 ms, 500 ≈ 1 s.
_SKETCH_CURVE_CAP = 150


def _add_sketch(sel, target):
    """Add a picked row to the viewport selection. A Sketch alone draws only its
    profiles, so a sketch goes in as its curves AND itself — the Sketch last, so
    whatever reads the last-selected item still gets the Sketch. Readers that act per
    entity collapse it back to one sketch (Sketch Picks.py). Raises like sel.add when
    the target itself can't be selected."""
    if isinstance(target, adsk.fusion.Sketch):
        try:
            curves = target.sketchCurves
            if sel.count + curves.count <= _SKETCH_CURVE_CAP:   # whole multi-pick
                for c in curves:
                    try:
                        sel.add(c)
                    except Exception:
                        pass
        except Exception:
            pass
    sel.add(target)


def _select_sketch(design, token, occ_token):
    """Select a sketch in the viewport WITHOUT entering sketch edit (single-click
    select). Entering edit stays on Enter via _enter_sketch."""
    if _extrude_pick(design, [(token, occ_token)], toggle=True):
        return
    target = _in_context(design, token, occ_token)
    if target is None:
        return
    sel = ui.activeSelections
    sel.clear()
    try:
        _add_sketch(sel, target)
    except Exception:
        pass


def _select_only(design, token, occ_token):
    """Right-click select: highlight the entity in the viewport WITHOUT the jump-down
    / activate that a normal browser click triggers. Works for any row type —
    component (the occurrence), body, or sketch."""
    ent = _find_entity(design, token)
    if not ent:
        return
    occ = _find_entity(design, occ_token) if occ_token else None
    target = ent
    try:
        if (occ and not isinstance(ent, adsk.fusion.Occurrence)
                and hasattr(ent, 'createForAssemblyContext')):
            target = ent.createForAssemblyContext(occ)
    except Exception:
        target = ent
    sel = ui.activeSelections
    sel.clear()
    try:
        _add_sketch(sel, target)
    except Exception:
        # Native body token isn't selectable on its own — fall back to the proxy.
        try:
            if occ and hasattr(ent, 'createForAssemblyContext'):
                _add_sketch(sel, ent.createForAssemblyContext(occ))
        except Exception:
            pass


def _select_many(design, items):
    """Browser-driven multi-select (Cmd-click / Shift-range): make the viewport
    selection exactly what the window has picked, in ONE command.

    The window used to send a single selectbody/selectsketch per click, so Fusion
    reported a one-row selection and the window's mirror (which replaces its own
    selection with the viewport's) wiped the accumulation — Cmd-click added up only
    for groups, which Fusion never sees.  Rows with no Fusion entity (groups,
    parameters) simply aren't in `items`; an empty list clears the selection.

    Entity resolution matches _select_only: the occurrence for a component row, the
    in-context proxy for a body/sketch under an occurrence.
    """
    if _extrude_pick(design, [(it.get('token', ''), it.get('occToken', ''))
                              for it in items], toggle=False):
        return
    sel = ui.activeSelections
    sel.clear()
    tokens = []
    for it in items:
        token = it.get('token', '')
        ent = _find_entity(design, token)
        if not ent:
            continue
        occ = _find_entity(design, it.get('occToken', '')) if it.get('occToken') else None
        target = ent
        try:
            if (occ and not isinstance(ent, adsk.fusion.Occurrence)
                    and hasattr(ent, 'createForAssemblyContext')):
                target = ent.createForAssemblyContext(occ)
        except Exception:
            target = ent
        try:
            _add_sketch(sel, target)
            tokens.append(token)
        except Exception:
            try:
                _add_sketch(sel, ent)
                tokens.append(token)
            except Exception:
                pass


def _delete_items(design, items):
    """Delete the rows the window asked for (Cmd+D).

    The window has a cursor and a selection of its own; neither is a Fusion
    selection, so a forwarded delete hotkey would act on whatever the VIEWPORT
    last had selected — usually nothing, which is why Cmd+D appeared to do
    nothing from this window.  So the rows are resolved here (same resolution as
    _select_only: the occurrence for a component row, the in-context proxy for a
    body/sketch under an occurrence) and deleted directly.

    Deleting the ACTIVE occurrence is refused by Fusion, so its parent is
    activated first — mirroring what you'd have to do by hand.
    """
    targets = []
    for it in items:
        ent = _find_entity(design, it.get('token', ''))
        if not ent:
            continue
        if _is_origin_entity(ent):
            continue        # fixed row — the window never offers these to delete/drag
        occ = _find_entity(design, it.get('occToken', '')) if it.get('occToken') else None
        target = ent
        try:
            if (occ and not isinstance(ent, adsk.fusion.Occurrence)
                    and hasattr(ent, 'createForAssemblyContext')):
                target = ent.createForAssemblyContext(occ)
        except Exception:
            target = ent
        targets.append(target)
    if not targets:
        return

    # Step out of anything being deleted before deleting it: an active occurrence
    # (or one of its ancestors) can't be removed while it's the edit target.
    try:
        active = design.activeOccurrence
    except Exception:
        active = None
    if active is not None:
        active_path = active.fullPathName
        for t in targets:
            if not isinstance(t, adsk.fusion.Occurrence):
                continue
            path = t.fullPathName
            if active_path == path or active_path.startswith(path + '+'):
                parent = t.assemblyContext
                if parent:
                    parent.activate()
                    fvs.remember_activation(parent)
                else:
                    _activate_token(design, ROOT_TOKEN)   # top-level row → step to root
                break

    # Every browser row is a whole item — an occurrence, a body or a sketch — so
    # delete it through the API rather than through Fusion's Delete command. The
    # command route is environment-dependent (DeleteCommand is disabled outside
    # Design; in Render only FusionDeleteCommand is enabled, and for a component it
    # opens a DELETE dialog and waits for OK, which reads as nothing happening).
    # deleteMe() just removes the row's item, in any workspace, with no dialog.
    # A component takes its bounds parameters and its timeline creation with it.
    # Both are worked out BEFORE the delete — afterwards there is no component to ask.
    # The parameters are found by their comment (Bounds.bounds_parameters); the timeline
    # group is the "Component" group New Component folded the creation into, which
    # would otherwise be left standing empty.
    params, groups, plane_parts = [], [], []
    if bnds is not None:
        for t in targets:
            if not isinstance(t, adsk.fusion.Occurrence):
                continue
            try:
                params += bnds.bounds_parameters(design, t.component)
            except Exception:
                pass
            try:
                grp = t.timelineObject.parentGroup
                if grp is not None:
                    groups.append(grp)
            except Exception:
                pass
        # A sketch the plane box placed carries the construction geometry it built
        # (offset plane / pivot axis / angled plane) in attributes on the sketch —
        # sketch_parts reads those. Worked out before the delete for the same reason
        # as the bounds params above: after deleteMe() there is nothing left to ask.
        for t in targets:
            if not isinstance(t, adsk.fusion.Sketch):
                continue
            try:
                native = t.nativeObject or t
                parts = bnds.sketch_parts(design, native)
                if parts:
                    plane_parts.append(parts)
            except Exception:
                pass

    # One shared rule for every delete path (Browser, Timeline, Delete Selection) —
    # read Deleting.py's header before changing it here.
    try:
        deleting.delete(ui, design, targets)
    except Exception:
        _tlog('delete failed:', traceback.format_exc())

    if bnds is not None:
        for parts in plane_parts:
            try:
                bnds.delete_parts(parts)
            except Exception:
                pass

    problems = []
    if params:
        _deleted, problems = bnds.delete_parameters(design, params=params)
    for grp in groups:
        # Ungroup rather than delete-with-contents: the group only ever held the
        # component's own creation steps, so what is left is nothing — and if the user
        # had folded anything else into it, that survives.
        try:
            if grp.isValid:
                grp.deleteMe(False)
        except Exception:
            pass
    if problems:
        _tlog('delete bounds params:', '; '.join(problems))
    return


# What a feature, sketch or piece of construction geometry can hold that came off a
# sketch: its profile(s), a path / rail / axis drawn in it, loft sections, and the
# definition references construction geometry is built on.
_SKETCH_REF_ATTRS = ('profile', 'profiles', 'path', 'guideRail', 'centerLine', 'axis',
                     'sketchCurves', 'curves', 'inputEntities', 'loftSections',
                     'centerLineOrRails', 'planarEntity', 'planarEntityOne',
                     'planarEntityTwo', 'linearEntity', 'linearEntityOne',
                     'linearEntityTwo', 'point', 'pointOne', 'pointTwo', 'edge')


def _native(e):
    try:
        return e.nativeObject or e
    except Exception:
        return e


def _parent_sketch_of(v):
    """The native sketch `v` was drawn in (a profile, curve, point, path entity or loft
    section), or None."""
    for hop in ('entity', None):             # PathEntity / LoftSection wrap the curve
        try:
            x = getattr(v, hop, None) if hop else v
            sk = x.parentSketch if x is not None else None
        except Exception:
            sk = None
        if sk is not None:
            return _native(sk)
    return None


def _refs_sketches(obj, targets):
    """True if anything `obj` is built from was drawn in one of `targets`."""
    for attr in _SKETCH_REF_ATTRS:
        try:
            v = getattr(obj, attr, None)
        except Exception:
            continue
        if v is None:
            continue
        vals = [v]
        try:
            if hasattr(v, 'count') and hasattr(v, 'item'):
                vals = [v.item(i) for i in range(v.count)]
        except Exception:
            vals = [v]
        for x in vals:
            sk = _parent_sketch_of(x)
            if sk is not None and any(sk == t for t in targets):
                return True
    return False


def _sketch_projects_from(sk, targets):
    """True if `sk` holds geometry projected / included from one of `targets`.

    POINTS only: a projected sketch curve always brings its reference end/centre points
    along, and those resolve in well under a millisecond — whereas a reference CURVE's
    referencedEntity, when it is a B-rep edge, measured ~0.9 s apiece (one sketch of 14
    projected edges cost 13 s)."""
    pts = sk.sketchPoints
    for i in range(pts.count):
        try:
            p = pts.item(i)
            if not p.isReference:
                continue
            ref = p.referencedEntity
        except Exception:
            continue
        ps = _parent_sketch_of(ref) if ref is not None else None
        if ps is not None and any(ps == t for t in targets):
            return True
    return False


def _group_uses(design):
    """{folder id: [feature names]} — the groups a feature follows: a live Combine run
    (its LINK's tg / og, AddIns/Combine) or an Extrude made with "Bodies are: Group"
    (josh.extrude/groups `g`) or Mirror (josh.mirror/follow `g`). The window warns
    before exploding one."""
    out = {}
    for group, name, ids in (('ScriptsCombine', 'link',
                              lambda d: [d.get('tg')] + list(d.get('og') or [])),
                             ('josh.extrude', 'groups', lambda d: list(d.get('g') or [])),
                             ('josh.mirror', 'follow', lambda d: list(d.get('g') or []))):
        try:
            attrs = design.findAttributes(group, name)
        except Exception:
            continue
        for a in attrs:
            try:
                gids = ids(json.loads(a.value))
                feat = a.parent.name
            except Exception:
                continue
            for gid in gids:
                if gid and feat not in out.setdefault(gid, []):
                    out[gid].append(feat)
    return out


def _sketch_dependents(design, tokens):
    """(label, kind, key) of what stands on the sketches in `tokens` — kind 'sketch' or
    'feature', which colours it in the menu, and a sketch's row key for its Go To — the
    features that use their
    profiles or curves, the sketches that project from them, construction geometry built
    on them — for the right-click Delete item. Anything that is itself one of those
    sketches doesn't count: the set is being deleted together. Only the timeline after
    the earliest of them is scanned (nothing earlier can reference it)."""
    targets, start = [], None
    for tok in tokens:
        ent = _find_entity(design, tok)
        sk = _native(ent) if ent is not None else None
        if sk is None or adsk.fusion.Sketch.cast(sk) is None:
            continue
        targets.append(sk)
        try:
            idx = sk.timelineObject.index
            start = idx if start is None else min(start, idx)
        except Exception:
            pass
    if not targets:
        return []
    out = []
    try:
        tl = design.timeline if design.designType == \
            adsk.fusion.DesignTypes.ParametricDesignType else None
    except Exception:
        tl = None
    if tl is not None:
        for i in range((start or 0) + 1, tl.count):
            try:
                o = tl.item(i)
                ent = o.entity
            except Exception:
                continue
            if ent is None or o.isGroup:
                continue
            if any(_native(ent) == t for t in targets):
                continue
            sk = adsk.fusion.Sketch.cast(ent)
            try:
                hit = (_sketch_projects_from(sk, targets) if sk is not None
                       else _refs_sketches(ent, targets))
            except Exception:
                hit = False
            if hit:
                out.append((_dep_label(ent, o.name), 'sketch' if sk is not None else 'feature',
                            (fvs.leaf_key_for(sk) or '') if sk is not None else ''))
        return out
    # Direct-modelling design: no timeline, so every sketch in every component.
    comps = design.allComponents
    for ci in range(comps.count):
        comp = comps.item(ci)
        for i in range(comp.sketches.count):
            sk = comp.sketches.item(i)
            if any(sk == t for t in targets):
                continue
            try:
                if _sketch_projects_from(sk, targets):
                    out.append((_dep_label(sk, sk.name), 'sketch', fvs.leaf_key_for(sk) or ''))
            except Exception:
                pass
    return out


def _dep_label(ent, name):
    """"Component / Name" — names repeat across components (two Sketch11s), so the
    component says which; the same shape as a parameter dependant's depWhere. The
    component is named as its row reads — the root's real name is the document's, and
    it has no row, so it is 'Top Level' (see root_name)."""
    try:
        name = name or ent.name
    except Exception:
        name = name or '?'
    try:
        comp = ent.parentComponent
        if comp == comp.parentDesign.rootComponent:
            return 'Top Level / %s' % name
        return '%s / %s' % (_display_name(comp.name), name)
    except Exception:
        return name


def _already_active(design, occ):
    """`occ` is already the active occurrence and no sketch is open. Entering one of
    its sketches then needs no activation first — re-activating only re-ran the
    snapshot/isolate dance and a full apply_policy (+ Dim pass) for no change, right
    before SketchActivate's own listener runs the policy again anyway."""
    try:
        ao = design.activeOccurrence
        return (ao is not None and ao.fullPathName == occ.fullPathName
                and not fvs.is_editing_sketch(design))
    except Exception:
        return False


def _enter_sketch(design, token, occ_token):
    ent = _find_entity(design, token)
    if not ent:
        return
    occ = _find_entity(design, occ_token) if occ_token else None
    if occ and not _already_active(design, occ):
        occ.activate()
        fvs.remember_activation(occ)
        fvs.apply_policy(design)
    sk = ent
    try:
        if occ and hasattr(ent, 'createForAssemblyContext'):
            sk = ent.createForAssemblyContext(occ)
    except Exception:
        sk = ent
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(sk)
    except Exception:
        pass
    cd = ui.commandDefinitions.itemById('SketchActivate')
    if cd:
        cd.execute()


def _reparent(design, token, target_token):
    """Move a component or body (dragged in the browser) into another component.
    `target_token` is the drop-target component's occurrence token; the item becomes
    a child of that component. Selection is intentionally left untouched."""
    ent = _find_entity(design, token)
    tgt = _find_entity(design, target_token)
    if not ent or not tgt:
        return
    target_occ = tgt if isinstance(tgt, adsk.fusion.Occurrence) else _occ_of(tgt)
    if target_occ is None:
        return
    try:
        if isinstance(ent, adsk.fusion.Occurrence):
            ent.moveToComponent(target_occ)
        elif isinstance(ent, adsk.fusion.BRepBody):
            ent.moveToComponent(target_occ)
    except Exception:
        pass


def _edit_sheetmetal(design, token):
    ent = _find_entity(design, token)
    occ = _occ_of(ent) if ent else None
    if occ:
        occ.activate()
        fvs.remember_activation(occ)
        fvs.apply_policy(design)
    cd = ui.commandDefinitions.itemById('SheetMetalDocumentRuleEdit')
    if cd:
        cd.execute()


def _texture_map(design, items):
    """Row menu → Texture Map…: select the held bodies, then open Fusion's own Texture
    Map Controls (TextureMappingCommand), which works on the selection."""
    _select_many(design, items)
    cd = ui.commandDefinitions.itemById('TextureMappingCommand')
    if cd:
        cd.execute()


# Row menu → Display Detail ▸ Auto / Low / Medium / High. The API has no per-body
# display detail, so each body goes through Fusion's own Display Detail Control
# (TessellationControlCmd) without the user touching it: select the body, start the
# command, and on the first tick that sees it running set its inputs by id (text
# commands) and commit it by starting SelectCommand — the same as clicking another
# tool. One body per run: the command's selection input takes exactly one.
# Input ids from UI.CurrentCommandInfo: LODAdaptiveInput / LODFixedInput (bools — Auto
# vs a fixed level) and infoLODValue (the Low/Medium/High dropdown).
_DETAIL_LEVELS = {'low': 'Low', 'medium': 'Medium', 'high': 'High'}
_detail_queue = []       # [(item, level)] still to do
_detail_open = None      # (level, started_at) while waiting for the command to come up


def _display_detail(design, items, level=None):
    global _detail_open
    if level not in _DETAIL_LEVELS and level != 'auto':
        # No level: the old Display Detail… — open the dialog on the selection.
        _select_many(design, items)
        cd = ui.commandDefinitions.itemById('TessellationControlCmd')
        if cd:
            cd.execute()
        return
    _detail_queue[:] = [(it, level) for it in items]
    _detail_open = None
    _detail_step(design, ui.activeCommand)


def _detail_step(design, active):
    """Advance the Display Detail queue by at most one step. Called every tick."""
    global _detail_open
    if _detail_open:
        level, t0 = _detail_open
        if active != 'TessellationControlCmd':
            if time.time() - t0 > 3.0:          # never came up: give the rest up
                _tlog('display detail: command never started (active %r)' % active)
                _detail_open = None
                del _detail_queue[:]
            return
        _detail_open = None
        try:
            if level == 'auto':
                app.executeTextCommand('Commands.SetBool LODAdaptiveInput 1')
            else:
                app.executeTextCommand('Commands.SetBool LODFixedInput 1')
                app.executeTextCommand('Commands.SetString infoLODValue ' + _DETAIL_LEVELS[level])
        except Exception:
            _tlog('display detail set failed:', traceback.format_exc())
        cd = ui.commandDefinitions.itemById('SelectCommand')
        if cd:
            cd.execute()
        return
    if not _detail_queue or active != 'SelectCommand':
        return
    it, level = _detail_queue.pop(0)
    _select_many(design, [it])
    cd = ui.commandDefinitions.itemById('TessellationControlCmd')
    if not cd:
        del _detail_queue[:]
        return
    cd.execute()
    _detail_open = (level, time.time())


def _set_edge_policy(design, comp_id, on):
    """Flag/unflag a component's 'hide edges while not active' setting.

    Persisted per document, so it survives a Fusion restart. Applied immediately —
    un-flagging restores that component's edges on the same tick.
    """
    if not comp_id:
        # The window sent the action but no component id — visible in /debug rather
        # than failing silently, which is how this cost an entire debugging round.
        try:
            _server_mod.set_debug(last_error='edgepolicy: empty compId')
        except Exception:
            pass
        return
    key = _doc_key()
    if on is None:
        edgepol.toggle(key, comp_id)
    else:
        edgepol.set_flag(key, comp_id, bool(on))
    _refresh_edge_flags(force=True)
    edgepol.apply(design, key, force=True, edit_component=_edit_sketch_owner(design))
    try:
        st = edgepol.status()
        if not st.get('ok') or not st.get('path'):
            _server_mod.set_debug(last_error='edgepolicy: native bridge %r' % (st,))
    except Exception:
        pass
    # The tree's change detection can't see a flag change, so force the next tick to
    # rebuild — otherwise the menu's tick could read stale for up to _FULL_BUILD_MAX.
    global _last_full_build
    _last_full_build = 0.0


def _set_edge_when(design, comp_id, trigger_id, on):
    """Add/remove one component from `comp_id`'s Hide Edges flyout: its edges go
    while any of the ticked components is drawn (Edge Policy's hideEdgesWhen).
    Both ids are Component.id, so a rename on either side changes nothing."""
    if not comp_id or not trigger_id:
        try:
            _server_mod.set_debug(last_error='edgewhen: empty id %r/%r' % (comp_id, trigger_id))
        except Exception:
            pass
        return
    key = _doc_key()
    edgepol.set_trigger(key, comp_id, trigger_id, on)
    _refresh_edge_flags(force=True)
    edgepol.apply(design, key, force=True, edit_component=_edit_sketch_owner(design))
    global _last_full_build
    _last_full_build = 0.0


def _comp_list(design):
    """[[id, name, [ancestor names, outermost first]]] of every component but the root,
    by name — what the Hide Edges flyout offers. Names as the tree draws them
    (_display_name), so two 'Steel's are as alike here as they look there; the page
    uses the ancestors to tell them apart. The path is the component's FIRST placement
    (one walk, parents before children), with disregarded wrappers left out like the
    tree does. A component with no placement gets no path."""
    paths = {}

    def walk(occs, path):
        for occ in occs:
            try:
                comp = occ.component
                cid = comp.id
            except Exception:
                continue
            name = _display_name(comp.name)
            if cid not in paths:
                paths[cid] = (name, path)
            inner = path if fvs.is_disregarded(comp) else path + [name]
            try:
                walk(occ.childOccurrences, inner)
            except Exception:
                pass

    try:
        walk(design.rootComponent.occurrences, [])
        root = design.rootComponent
        for c in design.allComponents:      # unplaced ones too
            try:
                if c != root and c.id not in paths:
                    paths[c.id] = (_display_name(c.name), [])
            except Exception:
                continue
    except Exception:
        pass
    out = [[cid, name, path] for cid, (name, path) in paths.items()]
    out.sort(key=lambda p: (p[1].lower(), [s.lower() for s in p[2]]))
    return out


def _set_hide_body(design, comp_id, on):
    """Hide this component's bodies entirely while you're not inside it.

    This is angle-independent: geometry that can't
    legitimately be seen from outside simply isn't drawn, so it can neither bleed
    through nor punch in front of anything. Use it for fully-enclosed internals.
    """
    if not comp_id:
        try:
            _server_mod.set_debug(last_error='hidebody: empty compId')
        except Exception:
            pass
        return
    key = _doc_key()
    if on is None:
        edgepol.toggle_hidden(key, comp_id)
    else:
        edgepol.set_hidden(key, comp_id, bool(on))
    _refresh_edge_flags(force=True)
    edgepol.apply(design, key, force=True, edit_component=_edit_sketch_owner(design))
    global _last_full_build
    _last_full_build = 0.0


# ── Appearances palette (the window's own — Fusion's dialog drag carries nothing) ──
# Fusion's Appearance dialog drags an empty Qt text/plain: which swatch it was is known
# only inside Fusion, so a drop on this window can't be told apart. The window draws its
# own swatches from this list instead, and a swatch dropped on a row posts `paint`.
_DIM_GROUP = 'DimOutsideActive'   # Dim Outside Active's "<name> · dim" copies — never offered
_LIB_ID = defapp.LIB_ID   # Fusion Appearance Library
_lib_cache = None
_swatch_of = defapp.swatch_of   # {'rgb': [r,g,b]} or {'tex': path}


# A face's own appearance reads FaceAppearanceSource (measured 2026-09-29 — Override
# never turned up); both are taken in case Fusion ever reports the other.
_FACE_SRC = (adsk.core.AppearanceSourceTypes.FaceAppearanceSource,
             adsk.core.AppearanceSourceTypes.OverrideAppearanceSource)


def _is_dim(a, dim):
    return a.name in dim or ' · dim' in a.name    # a clash makes "<name> · dim 2"


def _wearers(design, apps, dim):
    """Everything in the model that wears an appearance, as (kind, object, appearance,
    source): 'occ' an occurrence override, 'body' a native body (its own, or inherited
    from its material / occurrence — `source` says which), 'face' a face's own. Only
    components reachable from the root count — a component left in allComponents by a
    delete isn't in the model. A body wearing a Dim Outside Active copy yields the
    original (None when it was inherited)."""
    hook = sys.modules.get('dim_outside_active_hook')
    root = design.rootComponent
    comps, cseen = [root], {root.entityToken}
    for o in root.allOccurrences:
        try:
            if o.appearance:
                yield 'occ', o, o.appearance, None
        except Exception:
            pass
        c = o.component
        if c.entityToken not in cseen:
            cseen.add(c.entityToken)
            comps.append(c)
    amap = None
    for c in comps:
        for b in c.bRepBodies:
            try:
                a, src = b.appearance, b.appearanceSourceType
            except Exception:
                a, src = None, None
            if a and _is_dim(a, dim):
                orig = None
                if hook is not None and hasattr(hook, 'original'):
                    try:
                        orig = hook.original(design, b.entityToken, a)
                    except Exception:
                        orig = None
                else:
                    if amap is None:
                        amap = {at.name: at.value for at in
                                design.attributes.itemsByGroup(_DIM_GROUP)}
                    try:
                        rec = json.loads(amap.get(a.name) or '{}')
                    except Exception:
                        rec = {}
                    orig = next((x for x in apps if x.name == rec.get('name')
                                 and x.id == rec.get('id')), None)
                a = orig
            if a:
                yield 'body', b, a, src
            for f in b.faces:
                try:
                    if f.appearanceSourceType in _FACE_SRC and f.appearance:
                        yield 'face', f, f.appearance, None
                except Exception:
                    pass


def _worn(design, apps, dim):
    """Indexes into `apps` of the appearances something in the model actually wears
    (`_wearers`). Matched with == because name+id are not unique. Appearance.isUsed
    can't stand in: measured 2026-09-29 it said False for ABS (White) on an occurrence
    and True for copies nothing wore."""
    out, hits = set(), []
    for _k, _o, a, _s in _wearers(design, apps, dim):
        if not any(a == h for h in hits):
            hits.append(a)
    for a in hits:
        for i, x in enumerate(apps):
            if x == a:
                out.add(i)
                break
    return out


def _appearance_list(design):
    """In This Design — only what the model wears (`_worn`), one swatch per
    name+id+colour, dim copies left out — and the Fusion Appearance Library (cached for
    the session — it never changes)."""
    global _lib_cache
    dim = set()
    try:
        for at in design.attributes.itemsByGroup(_DIM_GROUP):
            dim.add(at.name)
    except Exception:
        pass
    apps = [design.appearances.item(i) for i in range(design.appearances.count)]
    worn = _worn(design, apps, dim)
    ours, seen = [], set()
    for i, a in enumerate(apps):
        if i not in worn or _is_dim(a, dim):
            continue
        sw = _swatch_of(a)
        key = (a.name, a.id, json.dumps(sw, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        ours.append(dict(sw, src='design', index=i, name=a.name, id=a.id))
    if _lib_cache is None:
        lib_out = []
        try:
            lib = app.materialLibraries.itemById(_LIB_ID)
            for i in range(lib.appearances.count if lib else 0):
                a = lib.appearances.item(i)
                lib_out.append(dict(_swatch_of(a), src='lib', index=i, name=a.name, id=a.id))
        except Exception:
            pass
        _lib_cache = lib_out
    return {'design': ours, 'library': _lib_cache, 'default': _default_spec()}


def _resolve_appearance(design, spec):
    """The design appearance a swatch names. Indexes shift as appearances are added, so
    the name+id travel with it and win; a library swatch is copied in on first use (or
    reuses a design appearance with the same name+id)."""
    name, aid, idx = spec.get('name', ''), spec.get('id', ''), spec.get('index', -1)
    apps = design.appearances
    if spec.get('src') == 'design':
        try:
            a = apps.item(int(idx))
            if a and a.name == name and a.id == aid:
                return a
        except Exception:
            pass
    for i in range(apps.count):
        a = apps.item(i)
        if a.name == name and a.id == aid:
            return a
    if spec.get('src') != 'lib':
        return None
    lib = app.materialLibraries.itemById(_LIB_ID)
    src = None
    try:
        src = lib.appearances.item(int(idx)) if lib else None
    except Exception:
        src = None
    if src is None or src.name != name:
        src = lib.appearances.itemByName(name) if lib else None
    return apps.addByCopy(src, name) if src else None


def _paint(design, spec, items):
    """Give every body row in `items` the swatch's appearance (the native body, so every
    instance — see Dim Outside Active's notes). Dim Outside Active's hook does the write
    when it's loaded, so a dimmed body re-dims in its new colour instead of keeping the
    old dim copy."""
    appearance = _resolve_appearance(design, spec)
    if appearance is None:
        _server_mod.set_debug(last_error='paint: no appearance %r' % (spec,))
        return
    bodies, seen = [], set()
    for it in items:
        ent = _find_entity(design, it.get('token', ''))
        b = adsk.fusion.BRepBody.cast(ent) if ent else None
        if b is None:
            continue
        b = b.nativeObject or b
        tok = b.entityToken
        if tok in seen:
            continue
        seen.add(tok)
        bodies.append(b)
    _paint_bodies(bodies, appearance)
    mat = sys.modules.get('material_hook')     # the Material add-in's Apply Material
    if mat is not None and bodies:
        try:
            mat.set_last(appearance)
        except Exception:
            pass
    _server_mod.set_appearances(_appearance_list(design))


def _paint_bodies(bodies, appearance):
    hook = sys.modules.get('dim_outside_active_hook')
    if hook is not None and hasattr(hook, 'paint'):
        hook.paint(bodies, appearance)
    else:
        for b in bodies:
            b.appearance = appearance


def _replace(design, old_spec, new_spec):
    """The palette's right-click Replace: everything wearing the `old_spec` swatch wears
    `new_spec` instead — occurrence overrides and face appearances in place, bodies
    (their own, or material-inherited) through `_paint_bodies` so a dimmed one re-dims.
    A body inheriting from an occurrence is left to the occurrence. The old swatch
    stands for every design appearance it deduped (same name+id+colour)."""
    old = _resolve_appearance(design, dict(old_spec, src='design'))
    new = _resolve_appearance(design, new_spec)
    if old is None or new is None or old == new:
        _server_mod.set_debug(last_error='replace: %r → %r' % (old_spec, new_spec))
        return
    dim = set()
    try:
        for at in design.attributes.itemsByGroup(_DIM_GROUP):
            dim.add(at.name)
    except Exception:
        pass
    apps = [design.appearances.item(i) for i in range(design.appearances.count)]
    osw = _swatch_of(old)
    olds = [x for x in apps if x.name == old.name and x.id == old.id and _swatch_of(x) == osw]
    hits = [w for w in _wearers(design, apps, dim) if any(w[2] == x for x in olds)]
    occ_src = adsk.core.AppearanceSourceTypes.OccurrenceAppearanceSource
    bodies = []
    for kind, obj, _a, src in hits:
        if kind == 'body':
            if src != occ_src:
                bodies.append(obj)
        else:
            obj.appearance = new
    if bodies:
        _paint_bodies(bodies, new)
    _server_mod.set_appearances(_appearance_list(design))


# Right-click a swatch → Edit…: Fusion's own Appearance dialog on a body wearing it.
# The API cannot write an appearance a body uses (that froze Fusion — never do it), so
# the native editor is the way in. A body is selected, AppearanceCommand started, and on
# the next ticks what the dialog exposes (UI.CurrentCommandInfo — never Toolkit.*) is
# logged to /tmp/browser_appearance_probe.log, which is how the route to the "In This
# Design" entry's own Edit gets found; until then the dialog opens with the body picked.
_appearance_probe = []      # [ticks left] while the dialog should be probed


def _edit_appearance(design, spec):
    a = _resolve_appearance(design, dict(spec, src='design'))
    if a is None:
        _server_mod.set_debug(last_error='editappearance: no appearance %r' % (spec,))
        return
    dim = set()
    try:
        for at in design.attributes.itemsByGroup(_DIM_GROUP):
            dim.add(at.name)
    except Exception:
        pass
    apps = [design.appearances.item(i) for i in range(design.appearances.count)]
    body = None
    for kind, obj, wa, _src in _wearers(design, apps, dim):
        if kind == 'body' and wa == a:
            body = obj
            break
    if body is None:
        for kind, obj, wa, _src in _wearers(design, apps, dim):
            if kind == 'occ' and wa == a:
                body = obj
                break
    if body is None:
        _server_mod.set_debug(last_error='editappearance: nothing wears %r' % a.name)
        return
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(body)
    except Exception:
        _tlog('editappearance: could not select', traceback.format_exc())
    cd = ui.commandDefinitions.itemById('AppearanceCommand')
    if cd:
        cd.execute()
        _appearance_probe[:] = [3]


def _appearance_probe_step(active):
    """Every tick while the dialog is being probed: once it is up, dump its inputs."""
    if not _appearance_probe:
        return
    if active != 'AppearanceCommand':
        _appearance_probe[0] -= 1
        if _appearance_probe[0] <= 0:
            del _appearance_probe[:]
        return
    del _appearance_probe[:]
    try:
        info = app.executeTextCommand('UI.CurrentCommandInfo')
        with open('/tmp/browser_appearance_probe.log', 'a') as f:
            f.write('%.0f\n%s\n\n' % (time.time(), info))
    except Exception:
        _tlog('appearance probe failed:', traceback.format_exc())


def _rename_appearance(design, spec, name):
    """The palette's Rename: every design appearance the swatch stands for (same
    name+id+colour — the swatch deduped them) takes `name`. Dim Outside Active knows a
    dim copy's original by name+id in its records (the fallback after a reload), so
    those follow — but only once nothing answers to the old name+id any more, or the
    record would switch to a different original."""
    a0 = _resolve_appearance(design, dict(spec, src='design'))
    if a0 is None or not name or a0.name == name:
        _server_mod.set_debug(last_error='renameappearance: %r → %r' % (spec, name))
        return
    old, aid, sw = a0.name, a0.id, _swatch_of(a0)
    apps = [design.appearances.item(i) for i in range(design.appearances.count)]
    for x in apps:
        if x.name == old and x.id == aid and _swatch_of(x) == sw:
            x.name = name
    if not any(x.name == old and x.id == aid for x in apps):
        for at in design.attributes.itemsByGroup(_DIM_GROUP):
            try:
                rec = json.loads(at.value or '{}')
            except Exception:
                continue
            if rec.get('name') == old and rec.get('id') == aid:
                rec['name'] = name
                at.value = json.dumps(rec)
    _server_mod.set_appearances(_appearance_list(design))


# Default for New Bodies — the palette's right-click item. Stored and read by
# Default Appearance.py (Browser/default_appearance.json, any swatch; why not Fusion's
# preference is there). `_paint_new_bodies` paints what New Bodies just saw appear — in
# every design, new ones too — only bodies that inherit their look. Extrude paints its
# own new bodies inside its build, so its preview wears the default too.
_DEFAULT_APP_FILE = defapp.FILE
_default_spec = defapp.spec


def _set_default_appearance(design, spec):
    """`spec` names a swatch (design or Library); None clears the default."""
    d = None
    if spec:
        if spec.get('src') == 'lib':
            lib = app.materialLibraries.itemById(_LIB_ID)
            a = None
            try:
                a = lib.appearances.item(int(spec.get('index', -1))) if lib else None
            except Exception:
                a = None
            if a is None or a.name != spec.get('name'):
                a = lib.appearances.itemByName(spec.get('name', '')) if lib else None
        else:
            a = _resolve_appearance(design, spec)
        if a is None:
            _server_mod.set_debug(last_error='defaultappearance: no appearance %r' % (spec,))
            return
        d = dict(_swatch_of(a), name=a.name, id=a.id)
    try:
        tmp = _DEFAULT_APP_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d or {}, f, indent=1)
        os.replace(tmp, _DEFAULT_APP_FILE)
    except Exception:
        _server_mod.set_debug(last_error=traceback.format_exc())
    _server_mod.set_appearances(_appearance_list(design))


_default_in = defapp.find_in


def _paint_new_bodies(design):
    """Bodies New Bodies just saw appear (`last_new`) that inherit their look and don't
    already wear the default get it — inside the Browser's one_step, so it undoes as one
    step after the command that made them."""
    keys = getattr(newbodies, 'last_new', None)
    d = _default_spec() if keys else None
    if d is None:
        return
    found = []
    for k in keys:
        b = fvs.leaf_by_key(design, k)
        b = adsk.fusion.BRepBody.cast(b) if b else None
        if b is not None:
            found.append(b)
    bodies = defapp.wanting(found, d)
    if not bodies:
        return
    appearance = _default_in(design, d)
    if appearance is None:
        _server_mod.set_debug(last_error='new-body paint: no appearance %r' % (d,))
        return
    _paint_bodies(bodies, appearance)
    _server_mod.set_appearances(_appearance_list(design))


def _apply_commands(design, mod, step):
    global _build_due, _reconcile_due
    did = False
    cmds = mod.pop_commands()
    if cmds:
        step.enter_context(tickguard.one_step('Browser'))
        # Most of these change nothing the change signature sees (a bulb, a name), so
        # the next settled tick rebuilds regardless.
        _build_due = True
        _reconcile_due = True
        tickguard.timeline_moved()
    for cmd in cmds:
        did = True
        try:
            a = cmd.get('action')
            if a == 'activate':
                _activate_token(design, cmd['token'])
            elif a == 'togglevis':
                _togglevis(design, cmd.get('tokens') or [cmd.get('token')])
            elif a == 'setvis':
                _setvis(design, cmd.get('tokens') or [cmd.get('token')],
                        bool(cmd.get('visible')))
            elif a == 'regroup':
                # A group was hidden, shown, or had something dragged in or out.
                # The window changed one flag; the bulbs follow from re-running the
                # policy, which is the only thing that writes them.
                fvs.apply_policy(design)
            elif a == 'stampbodies':
                _stamp_bodies(design, cmd.get('comps') or [])
            elif a == 'selectbody':
                _select_body(design, cmd['token'], cmd.get('occToken', ''))
            elif a == 'entersketch':
                _enter_sketch(design, cmd['token'], cmd.get('occToken', ''))
            elif a == 'selectsketch':
                _select_sketch(design, cmd['token'], cmd.get('occToken', ''))
            elif a == 'selectonly':
                _select_only(design, cmd['token'], cmd.get('occToken', ''))
            elif a == 'selectmany':
                _select_many(design, cmd.get('items') or [])
            elif a == 'extrudebodies':
                _extrude_bodies(design, cmd.get('items') or [], cmd.get('group') or '')
            elif a == 'reparent':
                _reparent(design, cmd['token'], cmd.get('targetToken', ''))
            elif a == 'delete':
                _delete_items(design, cmd.get('items') or [])
            elif a == 'sketchdeps':
                # Asked when a sketch's right-click menu opens; the answer rides /state.
                deps = _sketch_dependents(design, cmd.get('tokens') or [])
                mod.set_sketch_deps(cmd.get('seq'), [d[0] for d in deps],
                                    [d[1] for d in deps], [d[2] for d in deps])
            elif a == 'deselect':
                ui.activeSelections.clear()
            elif a == 'suspend':
                on = bool(cmd.get('on'))
                # suspend mode unlocks Lock Outside Active (Dim Outside Active add-in)
                hook = sys.modules.get('dim_outside_active_hook')
                if hook and hasattr(hook, 'set_suspended'):
                    hook.set_suspended(on)
                # …makes every nav a peek, and off jumps back (fvs § Suspend mode)
                fvs.set_suspended(design, on)
                _glow(on)
            elif a == 'peek':
                _peek_token(design, cmd.get('token') or ROOT_TOKEN)
            elif a == 'rename':
                _rename(design, cmd['token'], str(cmd.get('newName', '')).strip())
            elif a == 'disregard':
                _disregard(design, cmd['token'], str(cmd.get('mode', 'self')))
            elif a == 'command':
                cd = ui.commandDefinitions.itemById(cmd['cmd_id'])
                if cd:
                    cd.execute()
            elif a == 'sheetmetal':
                _edit_sheetmetal(design, cmd['token'])
            elif a == 'displaydetail':
                _display_detail(design, cmd.get('items') or [], cmd.get('level'))
            elif a == 'texturemap':
                _texture_map(design, cmd.get('items') or [])
            elif a == 'edgepolicy':
                _set_edge_policy(design, cmd.get('compId', ''), cmd.get('on'))
            elif a == 'edgewhen':
                _set_edge_when(design, cmd.get('compId', ''), cmd.get('trigger', ''),
                               bool(cmd.get('on')))
            elif a == 'hidebody':
                _set_hide_body(design, cmd.get('compId', ''), cmd.get('on'))
            elif a == 'appearances':
                mod.set_appearances(_appearance_list(design))
            elif a == 'paint':
                _paint(design, cmd.get('appearance') or {}, cmd.get('items') or [])
            elif a == 'replaceappearance':
                _replace(design, cmd.get('old') or {}, cmd.get('new') or {})
            elif a == 'renameappearance':
                _rename_appearance(design, cmd.get('appearance') or {},
                                   str(cmd.get('name', '')).strip())
            elif a == 'editappearance':
                _edit_appearance(design, cmd.get('appearance') or {})
            elif a == 'defaultappearance':
                _set_default_appearance(design, cmd.get('appearance'))
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())
    return did


# ── Viewport selection → browser ─────────────────────────────────────────────
def _same_component(a, b):
    """Component identity — Component is unhashable; compare by stable .id GUID,
    falling back to object equality."""
    try:
        return a.id == b.id
    except Exception:
        try:
            return a == b
        except Exception:
            return False


def _resolve_selection_entity(ent):
    """Map a viewport-selected entity to (token, owner_occ, matched):
      • token   — the tree-row token (native body/sketch token, or occurrence token)
      • owner_occ — the occurrence whose component should be activated on jump-down
                    (None means the root component); for a selected component it's the
                    occurrence itself (drill into the clicked component).
      • matched — False when the selection maps to no tree row (ignored).
    A face/edge/vertex resolves to its body; a sketch curve/point to its sketch."""
    if isinstance(ent, adsk.fusion.Occurrence):
        try:
            return ent.entityToken, ent, True
        except Exception:
            return '', None, False

    target = None
    if isinstance(ent, adsk.fusion.BRepBody):
        target = ent
    elif isinstance(getattr(ent, 'body', None), adsk.fusion.BRepBody):
        target = ent.body                       # face / edge / vertex
    elif isinstance(ent, adsk.fusion.Sketch):
        target = ent
    elif getattr(ent, 'parentSketch', None):
        target = ent.parentSketch               # sketch curve / point / profile
    if target is None:
        return '', None, False

    owner = getattr(target, 'assemblyContext', None)   # None => lives on root
    native = getattr(target, 'nativeObject', None) or target
    try:
        return native.entityToken, owner, True
    except Exception:
        return '', owner, True


def _sketch_from_selection():
    """The sketch the viewport selection belongs to — the sketch itself, one of its
    curves/points, or a profile (the filled region inside a closed sketch) — as
    (sketch, owner_occ); (None, None) when the selection isn't sketch geometry.
    owner_occ is None for a sketch on the root. Takes the LAST-selected item, so this
    still resolves from a multi-pick."""
    sel = ui.activeSelections
    if not sel.count:
        return None, None
    try:
        ent = sel.item(sel.count - 1).entity
    except Exception:
        return None, None
    sk = ent if isinstance(ent, adsk.fusion.Sketch) else getattr(ent, 'parentSketch', None)
    if sk is None:
        return None, None
    return sk, getattr(sk, 'assemblyContext', None)


def _enter_sketch_obj(design, sketch, owner_occ):
    """Activate the sketch's owning occurrence (isolate-aware) and open it for edit.
    From a master opened by Edit Original this goes back to the original sketch
    instead, like every other way out of it (see _activate_target)."""
    try:
        if fvs.is_editing_sketch(design) and fvs.request_master_return(design):
            return
    except Exception:
        pass
    if owner_occ is not None and not _already_active(design, owner_occ):
        if _activate_target(design, owner_occ):
            return
    sk = sketch
    try:
        if owner_occ is not None and hasattr(sketch, 'createForAssemblyContext'):
            sk = sketch.createForAssemblyContext(owner_occ)
    except Exception:
        sk = sketch
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(sk)
    except Exception:
        pass
    cd = ui.commandDefinitions.itemById('SketchActivate')
    if cd:
        cd.execute()


def _selection_rows(design):
    """Resolve the viewport selection into the tree-row tokens the browser should
    show as selected, plus the raw entities and the owner of the last-selected one.

    PURE: no activation, no sketch entry, no global state — so it is safe to call on
    EVERY tick, including the ones where the jump-down is held back. Mirroring what
    is selected must never wait on the decision of whether to act on it."""
    entities, tokens = [], []
    last_owner, last_matched = None, False
    # Geometry INSIDE the sketch you are editing (a line, a dimension — the dimension
    # tool and the value dialog both hold one selected) is not a pick of that sketch:
    # you are already in it. Resolving it up to its sketch lit the sketch's own row.
    open_sk, open_tok = None, ''
    try:
        editing, open_sk, open_tok = fvs._edit_sketch_info(design)
    except Exception:
        pass

    def _in_open_sketch(ent, tok):
        if open_sk is None or isinstance(ent, adsk.fusion.Sketch):
            return False
        if open_tok and tok == open_tok:
            return True
        try:
            ps = ent.parentSketch
            return (ps.nativeObject or ps) == open_sk
        except Exception:
            return False
    try:
        sel = ui.activeSelections
        items = []
        for i in range(sel.count):
            try:
                items.append(sel.item(i).entity)
            except Exception:
                pass
        # A sketch picked here is its curves + the Sketch (_add_sketch). The curves name
        # the same row, and resolving each one reads an entityToken — every tick, ~6
        # leaked undo records apiece — so they are dropped by == against the sketch first.
        picked_sk = [getattr(e, 'nativeObject', None) or e for e in items
                     if isinstance(e, adsk.fusion.Sketch)]
        for ent in items:
            if picked_sk and not isinstance(ent, adsk.fusion.Sketch):
                try:
                    ps = ent.parentSketch
                    if (ps.nativeObject or ps) in picked_sk:
                        continue
                except Exception:
                    pass
            tok, owner, matched = _resolve_selection_entity(ent)
            if not matched:
                continue
            if _in_open_sketch(ent, tok):
                continue
            entities.append(ent)
            if tok:
                tokens.append(tok)
            last_owner, last_matched = owner, True
    except Exception:
        return [], [], None, False

    seen, out = set(), []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out, entities, last_owner, last_matched


def _quiet_select(feats=()):
    """dock['quiet_select'](feature tokens): the selection that was just made is those
    features (and their sketches), to be shown, not navigated to — see _quiet_sel."""
    global _quiet_sel, _quiet_feats, _quiet_n
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    toks = frozenset(t for t in _selection_rows(design)[0] if t)
    _quiet_sel = toks
    _quiet_feats = [t for t in feats if t]
    _quiet_n = ui.activeSelections.count


_pick_cache = {'key': None, 'pick': {}}


def _sel_pick():
    """What the last viewport pick is, when it is a body or a part of one: {'kind':
    'body'} for a whole body, {'kind': 'part', 'feat': <token>} for a face / edge /
    vertex, with the feature Trace would pick first for it (Timeline's
    dock['trace_first'], '' when none). The page opens the body for either, and for a
    part picks that feature's row — the viewport keeps what was clicked. Traced once
    per pick, not per tick."""
    try:
        sel = ui.activeSelections
        if sel.count == 0:
            return {}
        ent = sel.item(sel.count - 1).entity
        native = getattr(ent, 'nativeObject', None) or ent
        if isinstance(native, adsk.fusion.BRepBody):
            return {'kind': 'body'}
        if not isinstance(native, (adsk.fusion.BRepFace, adsk.fusion.BRepEdge,
                                   adsk.fusion.BRepVertex)):
            return {}
        key = (sel.count, ent.entityToken)
    except Exception:
        return {}
    c = _pick_cache
    if c['key'] != key:
        feat = ''
        trace = (getattr(builtins, '_fusion_dock', None) or {}).get('trace_first')
        if trace:
            try:
                feat = trace(ent) or ''
            except Exception:
                feat = ''
        c.update(key=key, pick={'kind': 'part', 'feat': feat})
    return c['pick']


# ── Sync ─────────────────────────────────────────────────────────────────────
# Bounds parameters left behind by a component deleted (or renamed) somewhere we
# could not see — Fusion's own browser, the timeline. A scan of the user parameters
# plus one pass over allComponents, at most every few seconds, only while the timeline
# is at its end, and only once a command has ended (or another document came up) since
# the last one: nothing else can delete or rename a component. Unconditional, it was
# 0.5–2.3 s every 5 s on 260612 (2026-10-07). Rolled back, a component that WILL come
# back reads as gone, and deleting its parameters then would be exactly the wrong thing.
_last_reconcile = 0.0
_RECONCILE_EVERY_S = 5.0
_reconcile_due = True
_reconcile_doc = None


def _reconcile_bounds(design):
    global _last_reconcile, _reconcile_due, _reconcile_doc
    if bnds is None:
        return
    try:
        doc = design.parentDocument
        if _reconcile_doc is None or not _reconcile_doc.isValid or not (_reconcile_doc == doc):
            _reconcile_doc = doc
            _reconcile_due = True
    except Exception:
        pass
    if not _reconcile_due:
        return
    now = time.time()
    if now - _last_reconcile < _RECONCILE_EVERY_S:
        return
    _last_reconcile = now
    try:
        count, marker = tickguard.timeline_pos(design)
        if marker != count:
            return                             # still due: run once it's back at the end
    except Exception:
        return
    _reconcile_due = False
    try:
        renamed, deleted, problems = bnds.reconcile_parameters(design)
    except Exception:
        _tlog('reconcile bounds:', traceback.format_exc().replace('\n', ' | '))
        return
    if _server_mod:
        for was, new in renamed:
            _server_mod.push_ui_action('BrowserParamEditedCmd', {'name': was, 'newName': new})
    if renamed or deleted or problems:
        _tlog('reconcile bounds: renamed=%s deleted=%s problems=%s' % (renamed, deleted, problems))


# ── Where you were, per document ─────────────────────────────────────────────
# Fusion always reopens a document at the root, whatever was active when it was saved
# or closed (Flush Undo works round the same thing for its own reopen). So every tick
# records the active occurrence's path under the document's lineage, and the first
# idle tick after a document is (re)opened activates that path again — through
# _activate_target, so it is the same as navigating there. A reopen is told apart by
# the Document object: the one remembered for the lineage is invalid once closed.
_DOC_ACTIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doc_active.json')
_doc_active = None          # {lineage: occurrence fullPathName, '' = root}
_doc_seen = {}              # lineage -> the Document object last handled


def _doc_active_load():
    global _doc_active
    if _doc_active is None:
        try:
            with open(_DOC_ACTIVE_FILE, 'r', encoding='utf-8') as f:
                _doc_active = json.load(f)
            if not isinstance(_doc_active, dict):
                _doc_active = {}
        except Exception:
            _doc_active = {}
    return _doc_active


def _doc_active_track(design):
    lin = fvs._lineage(design)
    if not lin or lin.startswith('__'):
        return                              # unsaved: nothing durable to key it on
    try:
        doc = design.parentDocument
        prev = _doc_seen.get(lin)
        reopened = prev is None or not prev.isValid or not (prev == doc)
    except Exception:
        return
    data = _doc_active_load()
    if reopened:
        # Only from an idle, sketch-free root; otherwise wait for the next tick —
        # unless something (Flush Undo) has already put you somewhere.
        try:
            if design.activeComponent != design.rootComponent:
                _doc_seen[lin] = doc
                return
            if ui.activeCommand != 'SelectCommand' or fvs.is_editing_sketch(design):
                return
        except Exception:
            return
        _doc_seen[lin] = doc
        path = data.get(lin) or ''
        if path:
            target = None
            try:
                for occ in design.rootComponent.allOccurrences:
                    if occ.fullPathName == path:
                        target = occ
                        break
            except Exception:
                target = None
            if target:
                try:
                    _activate_target(design, target)
                except Exception:
                    pass
        return
    try:
        occ = fvs.recall_active_occ(design)
        path = occ.fullPathName if occ else ''
    except Exception:
        return
    if data.get(lin, '') != path:
        data[lin] = path
        try:
            tmp = _DOC_ACTIVE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, _DOC_ACTIVE_FILE)
        except Exception:
            pass


# ── Environment backstop ─────────────────────────────────────────────────────
# Tranquility Blue in light appearance, Dark Sky in dark appearance. Appearance
# Toggle applies this itself on flip, but corrects drift on idle ticks (Fusion
# starts up in whichever environment it quit in).
SKETCH_ENV, BASE_ENV = 'Tranquility Blue', 'Dark Sky'
_APPEARANCE_FILE = '/Users/josh/Code/Scripts/fusion_ui/appearance.json'
_env_checked = 0.0
_env_applied = None   # last environment pushed through an ENABLED ViewEnvCommand


def _env_enforce(design):
    global _env_checked
    now = time.time()
    if now - _env_checked < 0.5:
        return
    _env_checked = now
    try:
        if ui.activeCommand != 'SelectCommand' or fvs.master_hop_in_progress():
            return
    except Exception:
        return
    light = False
    try:
        with open(_APPEARANCE_FILE, 'r', encoding='utf-8') as f:
            light = json.load(f).get('mode') == 'light'
    except Exception:
        pass
    want = SKETCH_ENV if light else BASE_ENV
    cd = ui.commandDefinitions.itemById('ViewEnvCommand')
    ctl = cd.controlDefinition if cd else None
    lcd = adsk.core.ListControlDefinition.cast(ctl) if ctl else None
    if not lcd:
        return
    target = None
    for item in lcd.listItems:
        # .strip(): the items are indented under the "Theme Environments" /
        # "Legacy Environments" headers, so the name is '    Dark Sky'. An exact
        # == matched nothing and this backstop silently never fired.
        if item.name.strip() == want:
            target = item
            break
    if target is None:
        return
    # isSelected alone is NOT proof the viewport followed. The Render workspace
    # leaves ViewEnvCommand disabled, and a selection assigned while it is disabled
    # is recorded in the list but never applied — the list reads "Dark Sky" while
    # the viewport is still Tranquility Blue, and it only lands when you next enter
    # Design. So skip only when WE applied it through an enabled control, and do the
    # assignment with isEnabled forced on, restoring it straight after.
    global _env_applied
    if target.isSelected and _env_applied == want:
        return
    was = ctl.isEnabled
    try:
        if not was:
            ctl.isEnabled = True
        target.isSelected = True
        _env_applied = want
    finally:
        if not was:
            try:
                ctl.isEnabled = False
            except Exception:
                pass


def _sync(design, step):
    """`step` is an ExitStack the handler closes after the tick: the undo group is
    opened on it only once the tick has real work (see SyncHandler)."""
    global _quiet_sel, _quiet_feats
    mod = _server_mod
    if mod is None:
        return
    try:
        _doc_active_track(design)
    except Exception:
        pass
    try:
        _env_enforce(design)
    except Exception:
        pass
    # Tell the event tap whether we're idle (so it only swallows right-clicks — for the
    # Go-Up gesture — while idle, never during a running command's menu). Every tick.
    try:
        active = ui.activeCommand
        _write_selectmode(active == 'SelectCommand')
        _restore_select(active)
        _detail_step(design, active)
        _appearance_probe_step(active)
    except Exception:
        pass
    did_cmd = _apply_commands(design, mod, step)
    _sync_construction_vis()
    _reconcile_bounds(design)

    # Per-component edge policy. Runs every tick but short-circuits unless the
    # flagged set or the active-component chain actually changed, so the common
    # case costs one set comparison. Also refreshes _edge_flags, which the tree
    # build below reads to render the menu checkbox.
    # Refresh the flag cache EVERY tick — the tree build below renders the menu
    # checkboxes from it, so gating this on idle left them showing stale values.
    # Only the native apply() is idle-gated: during a running command the scene graph
    # is being rebuilt, and that's the worst moment to touch scene nodes. Deferring
    # the apply costs at most one tick.
    try:
        _edge_key = _refresh_edge_flags()
        if ui.activeCommand == 'SelectCommand':
            edgepol.apply(design, _edge_key, edit_component=_edit_sketch_owner(design))
    except Exception:
        pass

    # What a CLICK does (jump down, a two-finger tap's Go Up, a three-finger tap's Go to
    # Top) is the Click Actions add-in's now — fusion_ui/clicks.json, Control Room ▸
    # Fusion ▸ Clicks. This tick only MIRRORS the viewport selection: what is selected
    # is a fact about the document and is always safe to show. A nav command Click
    # Actions ran lands here as an ordinary activation on a later tick.
    sel_tokens = _selection_rows(design)[0]
    _publish_click()
    _publish_extrude()
    peek_tok = _publish_peek(design)
    if _quiet_sel is not None and (frozenset(t for t in sel_tokens if t) != _quiet_sel
                                   or ui.activeSelections.count != _quiet_n):
        _quiet_sel, _quiet_feats = None, []
    # A body, or a part of one: the page opens it, and for a part picks the traced
    # feature's row (see _sel_pick). Not for a Trace selection, which picks its own.
    sel_pick = _sel_pick() if _quiet_sel is None else {}

    # An async command (cd.execute — e.g. a toggle / Go Up) hasn't settled yet, so skip the
    # rebuild this tick (it refreshes next tick, once the command has applied).
    if did_cmd:
        return

    show_bodies   = fvs.bodies_visible()
    show_sketches = fvs.sketches_visible()
    toggles = {
        'bodies':   show_bodies,
        'sketches': show_sketches,
        'origins':  fvs.origins_visible(),
        'isolate':  fvs.isolate_is_active(),
    }
    # True while a sketch is open for edit — lets the window's D key forward to
    # Go Up a Level (exit the sketch) instead of doing tree nav, and moves the
    # bold+underline emphasis off the active component onto the edited sketch row.
    editing_sketch = False
    edit_sketch_token = ''
    try:
        editing_sketch = fvs.is_editing_sketch(design)
        if editing_sketch:
            sk = fvs.recall_edit_sketch(design) or fvs.edited_sketch_from_marker(design)
            if sk:
                native = sk.nativeObject or sk
                edit_sketch_token = native.entityToken
    except Exception:
        pass
    global _last_edit_sketch_token
    if edit_sketch_token and edit_sketch_token != _last_edit_sketch_token:
        _note_edited_sketch(design, edit_sketch_token)
    _last_edit_sketch_token = edit_sketch_token

    # Cheap change signature — skip the full tree rebuild when nothing the browser shows
    # has changed and no command has ended since the last one (see _build_due).
    global _last_sig, _last_full_build, _build_due
    try:
        active_id = design.activeComponent.id
    except Exception:
        active_id = ''
    # The timeline marker is in it too: rolling back changes which components exist,
    # and the window hides whatever is tied to one that doesn't (rolledBack below).
    try:
        count, marker = tickguard.timeline_pos(design)
        tl_pos = (marker, count)
    except Exception:
        tl_pos = None
    rolled_back = bool(tl_pos) and tl_pos[0] != tl_pos[1]
    sig = (active_id, tuple(sel_tokens), tuple(sorted(toggles.items())), editing_sketch,
           edit_sketch_token, tl_pos, _quiet_sel is not None, tuple(_quiet_feats),
           tuple(sorted(sel_pick.items())), _tree_flags(), peek_tok)
    now = time.time()
    if sig == _last_sig and not _build_due and (
            (now - _last_full_build) < _FULL_BUILD_MAX
            or _input_idle() < _FORCED_BUILD_IDLE_S):
        return
    _last_sig = sig
    _last_full_build = now
    _build_due = False

    step.enter_context(tickguard.one_step('Browser'))   # the rebuild reads every token
    _t0 = time.perf_counter()
    tree, active_token = _build_tree(design, show_bodies, show_sketches)
    _perf_record((time.perf_counter() - _t0) * 1000.0)

    doc_id = ''
    try:
        doc = app.activeDocument
        if doc:
            try:
                df = doc.dataFile
                doc_id = df.id if df else ''
            except Exception:
                doc_id = ''
            if not doc_id:
                doc_id = '__unsaved__:' + (doc.name or '')
    except Exception:
        pass

    # The root component has no row of its own, but it can still OWN a structured
    # parameter name (it is one of the owners the value dialog offers), so the window
    # is told its name — see server.py's _state["rootName"]. Not its real name (that is
    # the document's, and changes with it): always "Top Level", matching the owner the
    # value dialog offers (Dimension Value.py's ROOT_OWNER).
    root_name = 'Top Level'

    mod.update_state(tree, active_token, toggles, doc_id, sel_tokens, editing_sketch,
                     edit_sketch_token, _last_click_ts(), root_name, rolled_back,
                     sel_quiet=_quiet_sel is not None, sel_feats=_quiet_feats,
                     sel_pick=sel_pick, comps=_comp_list(design))
    try:
        mod.set_group_uses(_group_uses(design))
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())

    # Bodies a command just made go into the group of the body they came from (New
    # Bodies.py says which; the window files them — it owns folders.json).
    try:
        filing = newbodies.observe(design, doc_id, tree, ui.activeCommand == 'SelectCommand')
        if filing:
            mod.push_ui_action('BrowserFileNewBodiesCmd', filing)
        _paint_new_bodies(design)      # and wear the default for new bodies
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())


HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'


def _hotkeys():
    """The shared action registry (fusion_ui/hotkeys.py), via sys.modules so every
    add-in shares ONE registry and ONE router, and re-exec'd when it changes because
    it lives outside the AddIns folder where Reload Recent AddIns cannot reach it."""
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod


def _declare_to_registry(actions):
    """Tell the router these keys are already spoken for, and in what domain.

    Declared NATIVE: this add-in implements them itself, in this very handler, so the
    router must never run or cancel one. What it buys is twofold. The keys they hold
    (a, c, r, f, d) become ANCHORS — an action in a mutually exclusive domain can be
    added to one with a bindings.json edit and no trip through Fusion's dialog, which
    is otherwise all-or-nothing and would take the key off Go In. And their domain is
    written down: browsing the tree is navigation, which is meaningless while a command
    is running, so anything scoped to a running command can share the key.

    Inert until something else claims one of these keys — a native claimant that wins
    is left completely alone.
    """
    try:
        hk = _hotkeys()
        for a in actions:
            cid = a.get('id')
            if not cid:
                continue
            hk.declare(cid, a.get('name') or cid, a.get('desc') or '',
                       owner='Browser', when=a.get('when', {'command': 'none'}),
                       native=True)
        hk.attach()
    except Exception:
        _tlog('could not declare to registry', traceback.format_exc().replace('\n', ' | '))


def _register_forwarded_actions():
    """Find-or-create a CommandDefinition per browser-homed action in the manifest.

    No toolbar control: these exist purely to give the window's actions a name Fusion
    can bind a key to. A definition that already exists is reused, so reloading the
    add-in never orphans a binding — Fusion's keymap points at the id, and the id is
    stable.
    """
    global _browser_action_ids
    mod = _server_mod
    if mod is None:
        return
    _declare_to_registry(mod.read_actions())
    ids = set()
    for a in mod.read_actions():
        if a.get('home') != 'browser':
            continue
        cid = a.get('id')
        if not cid:
            continue
        try:
            cd = ui.commandDefinitions.itemById(cid)
            if not cd:
                cd = ui.commandDefinitions.addButtonDefinition(
                    cid, a.get('name') or cid, a.get('desc') or '')
            # Fallback path only — the commandStarting veto normally stops the command
            # long before it is created. Registered so a veto that fails to take
            # forwards the action anyway instead of doing nothing at all.
            h = ForwardedCreatedHandler(cid)
            cd.commandCreated.add(h)
            handlers.append(h)
            ids.add(cid)
        except Exception:
            _tlog('could not register action', cid, traceback.format_exc().replace('\n', ' | '))
    _browser_action_ids = ids
    try:
        hk = _hotkeys()
        if hasattr(hk, 'set_direct'):
            for cid in ids:
                hk.set_direct(cid, lambda c=cid: _direct_press(c))
    except Exception:
        _tlog('could not set direct', traceback.format_exc().replace('\n', ' | '))


# ── Lifecycle ────────────────────────────────────────────────────────────────
def run(context):
    global _custom_evt, _active
    try:
        _start_server()
        _register_forwarded_actions()
        # Shared with AddIns/Timeline, which may have created the dict first.
        dock = getattr(builtins, '_fusion_dock', None)
        if not isinstance(dock, dict):
            dock = builtins._fusion_dock = {}
        dock['show_dock'] = _show_dock
        dock['quiet_select'] = _quiet_select
        _active = True
        _stop_evt.clear()
        # Suspend mode never outlives the add-in: with no window to switch it back off,
        # a stale flag would turn every nav key into a peek. A suspended window
        # re-asserts it with its next peek (_peek_token).
        try:
            fvs.set_suspended(adsk.fusion.Design.cast(app.activeProduct), False)
        except Exception:
            pass
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = SyncHandler()
        _custom_evt.add(h)
        handlers.append(h)
        threading.Thread(target=_poller, daemon=True).start()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        hc = CommandCreatedHandler()
        cmd_def.commandCreated.add(hc)
        handlers.append(hc)

        # Intercept the toggle before it becomes the active command, so pressing
        # the browser hotkey mid-command no longer drops out of that command.
        global _dock_starting_handler
        _dock_starting_handler = HotkeyStartingHandler()
        ui.commandStarting.add(_dock_starting_handler)
        handlers.append(_dock_starting_handler)

        global _terminated_handler
        _terminated_handler = CommandTerminatedHandler()
        ui.commandTerminated.add(_terminated_handler)
        handlers.append(_terminated_handler)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        # Viewport-side rename / toggle-visibility commands (no toolbar button — bind
        # them to Cmd+R / Cmd+E via AnyShortcut). Find-or-create so reloads are safe.
        rn = ui.commandDefinitions.itemById(RENAME_CMD_ID)
        if not rn:
            rn = ui.commandDefinitions.addButtonDefinition(
                RENAME_CMD_ID, 'Browser: Rename Selection',
                'Rename the selected item (or the active component). Bind to Cmd+R via AnyShortcut.')
        hr = RenameCreatedHandler()
        rn.commandCreated.add(hr)
        handlers.append(hr)

        tv = ui.commandDefinitions.itemById(TOGGLEVIS_CMD_ID)
        if not tv:
            tv = ui.commandDefinitions.addButtonDefinition(
                TOGGLEVIS_CMD_ID, 'Browser: Toggle Visibility',
                'Toggle visibility of the selected item (or active component). Bind to Cmd+E via AnyShortcut.')
        ht = ToggleVisCreatedHandler()
        tv.commandCreated.add(ht)
        handlers.append(ht)

        es = ui.commandDefinitions.itemById(ENTERSKETCH_CMD_ID)
        if not es:
            es = ui.commandDefinitions.addButtonDefinition(
                ENTERSKETCH_CMD_ID, 'Browser: Edit Selected Sketch',
                'Open the selected sketch for editing. Bind to Return.')
        he = EnterSketchCreatedHandler()
        es.commandCreated.add(he)
        handlers.append(he)

        # Both are vetoed at commandStarting and acted on there (see
        # HotkeyStartingHandler); the definition exists so they can be bound.
        for cid, name, desc in (
                (SWAP_CMD_ID, 'Browser: Swap With Fusion\'s',
                 'Show the dock and hide Fusion\'s Browser, or the other way round.'),
                (RECENT_SKETCH_CMD_ID, 'Sketch: Edit Previous',
                 'Open the most recently edited sketch other than the one open now.'),
                (ESCDESEL_CMD_ID, 'Escape / Deselect',
                 'Esc while anything is running; otherwise Deselect (viewport and Browser).'),
                (SUPERESC_CMD_ID, 'Super Escape',
                 'Escape everything open at once: the running command and its dialog, '
                 'and whatever our windows have open (the Value dialog, the dock\'s menus).')):
            if not ui.commandDefinitions.itemById(cid):
                ui.commandDefinitions.addButtonDefinition(cid, name, desc)

        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Fusion Browser failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _custom_evt, _active
    try:
        _active = False
        _glow(False)
        dock = getattr(builtins, '_fusion_dock', None)
        if isinstance(dock, dict):
            dock['show_dock'] = None
        try:
            _hotkeys().withdraw('Browser')
        except Exception:
            pass
        _stop_evt.set()
        global _dock_starting_handler
        if _dock_starting_handler is not None:
            try:
                ui.commandStarting.remove(_dock_starting_handler)
            except Exception:
                pass
            _dock_starting_handler = None
        global _terminated_handler
        if _terminated_handler is not None:
            try:
                ui.commandTerminated.remove(_terminated_handler)
            except Exception:
                pass
            _terminated_handler = None
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


# ── Event handlers ───────────────────────────────────────────────────────────
class SyncHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _tick_pending
        _tick_pending = 0.0
        if not _active:
            return
        if _super_esc['left'] > 0:         # a key press, not model work: before the guard
            try:
                _super_escape_step()
            except Exception:
                _super_esc['left'] = 0
        # Stand down if anything else is already in the model — a Parameters tick
        # mid-rename, a nav add-in restoring visibility, or this very handler
        # re-entered because the recompute it triggered pumped the task queue.
        # Walking a half-rebuilt model is what the .hang spindumps caught us doing;
        # see Tick Guard.py. Missing one 70 ms frame costs nothing.
        with tickguard.tick('browser') as go:
            if not go:
                return
            mod = _server_mod
            if mod:
                mod.bump_notify()
            try:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if mod:
                    # tick_guard rises whenever a tick stood down because something
                    # else was in the model — the number is the fix doing its job, not
                    # a fault. `stuck` must stay 0; anything else means a handler died
                    # without releasing the latch (Tick Guard.py's backstop).
                    mod.set_debug(design_found=bool(design),
                                  tick_guard=tickguard.stats())
                if design:
                    # One undo step per tick (Tick Guard.py § One undo step): a jump
                    # was ~50 steps, each making the next write slower. A tick that
                    # only reads is aborted, which also drops the tracking records
                    # its entityToken reads would otherwise leave in the document.
                    # Opened lazily, only on a tick with window commands or a tree
                    # rebuild: Transaction.Start notifies every bulletin in the doc,
                    # and doing it on all 70 ms ticks pinned the main thread at 100%
                    # (2026-09-28). An idle tick's few selection-token records land
                    # in Session, which Flush Undo's idle trim clears.
                    with contextlib.ExitStack() as step:
                        _sync(design, step)
                elif mod:
                    mod.update_state([], '', {}, '')
            except Exception:
                if mod:
                    mod.set_debug(last_error=traceback.format_exc())


_terminated_handler = None


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """Any command that ends may have changed what the tree shows in a way the change
    signature can't see (a bulb, a rename, a new body): rebuild on the next tick. And
    it may have moved the timeline, so the shared reading is dropped too."""
    def notify(self, args):
        global _build_due, _reconcile_due
        try:
            if args.commandId in _NO_TREE_CMDS:
                return
        except Exception:
            pass
        _build_due = True
        _reconcile_due = True
        try:
            tickguard.timeline_moved()
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


TOGGLE_LOG = '/tmp/fusion_dock_toggle.log'
_dock_starting_handler = None


def _tlog(*parts):
    try:
        with open(TOGGLE_LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _dock_running():
    try:
        return subprocess.run(['pgrep', '-x', APP_PROC], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


# Which browser is the CHOSEN one right now — 'dock' (default) or 'native' — and,
# while native, whether Fusion's own browser is currently shown. Persisted in
# Shared State.json (the same file fvs and the toggle add-ins use) so the `b` key
# and Minimal UI agree with whatever `y` last swapped to, across add-in reloads.
def _browser_kind():
    if fvs is None:
        return 'dock'
    return fvs._read_shared_state().get('browser_kind', 'dock')


def _set_browser_kind(kind, native_shown=None):
    if fvs is None:
        return
    state = fvs._read_shared_state()
    state['browser_kind'] = kind
    if native_shown is not None:
        state['native_browser_shown'] = bool(native_shown)
    fvs._write_shared_state(state)


def _toggle_browser():
    """Show/hide whichever browser is currently chosen — the dock, or Fusion's own.

    FusionDock is LSUIElement=true, so it never takes focus away from Fusion; the
    window appearing has no effect on whatever command is running. Fusion's own
    browser is driven through its Show/Hide commands (see Minimal UI).
    """
    try:
        if _browser_kind() == 'native':
            shown = fvs._read_shared_state().get('native_browser_shown', True) if fvs else True
            cd = ui.commandDefinitions.itemById('HideBrowserCommand' if shown
                                                else 'ShowBrowserCommand')
            if cd:
                cd.execute()
            _set_browser_kind('native', native_shown=not shown)
            return True

        running = _dock_running()
        if running:
            subprocess.Popen(['pkill', '-x', APP_PROC])
        else:
            subprocess.Popen(['open', '-a', APP_PATH])
        return True
    except Exception:
        if ui:
            ui.messageBox(
                'Could not toggle the browser.\nBuild FusionDock first:\n'
                '  /Users/josh/Code/Scripts/fusion_dock/FusionDockApp/build.sh')
        return False


def _swap_browsers():
    """Our browser or Fusion's: show one, hide the other, and remember which is the
    chosen one so `_toggle_browser` (the `b` key) keeps acting on it.

    cd.execute() only queues, which is fine: the two halves need no ordering
    between them.

    From timeline mode it drops back to browser mode as well — the timeline only
    ever shows in the dock, so the swap lands on Fusion's browser.
    """
    try:
        dock = getattr(builtins, '_fusion_dock', None)
        if (isinstance(dock, dict) and dock.get('mode') == 'timeline'
                and callable(dock.get('set_mode'))):
            dock['set_mode']('browser')
        if _browser_kind() == 'native':
            cd = ui.commandDefinitions.itemById('HideBrowserCommand')
            if cd:
                cd.execute()
            _set_browser_kind('dock', native_shown=False)
            subprocess.Popen(['open', '-a', APP_PATH])
        else:
            subprocess.Popen(['pkill', '-x', APP_PROC])
            cd = ui.commandDefinitions.itemById('ShowBrowserCommand')
            if cd:
                cd.execute()
            _set_browser_kind('native', native_shown=True)
    except Exception:
        _tlog('swap browsers failed:', traceback.format_exc().replace('\n', ' | '))


def _show_dock():
    """Make the dock the chosen browser if `y` had swapped to Fusion's. Handed to the
    Timeline add-in (builtins._fusion_dock['show_dock']): timeline mode needs the dock."""
    if _browser_kind() == 'native':
        _swap_browsers()


# The sketches edited this session, most recent first, as (durable key, token). Pushed
# from the sync tick whenever the edited sketch changes, so it costs nothing extra: the
# tick already works out which sketch is open. Session-only on purpose — a token rots
# between sessions and the key is only there to recognise the same sketch again.
_sketch_history = []
_SKETCH_HISTORY_MAX = 20
_last_edit_sketch_token = ''


def _note_edited_sketch(design, token):
    if not token:
        return
    try:
        ent = _find_entity(design, token)
        key = fvs.leaf_key_for(ent) if ent is not None else ''
    except Exception:
        key = ''
    if not key:
        key = token
    global _sketch_history
    _sketch_history = [h for h in _sketch_history if h[0] != key]
    _sketch_history.insert(0, (key, token))
    del _sketch_history[_SKETCH_HISTORY_MAX:]


_esc_cg = None


def _post_escape():
    """A real Esc key, posted to Fusion itself (not the HID stream, so remapd never sees
    it to re-route). Flags cleared: the chord that got us here may still be held."""
    global _esc_cg
    try:
        import ctypes
        if _esc_cg is None:
            cg = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
            cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
            cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
            cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
            cg.CFRelease.argtypes = [ctypes.c_void_p]
            _esc_cg = cg
        for down in (True, False):
            ev = _esc_cg.CGEventCreateKeyboardEvent(None, 53, down)   # kVK_Escape
            if not ev:
                return
            _esc_cg.CGEventSetFlags(ev, 0)
            _esc_cg.CGEventPostToPid(os.getpid(), ev)
            _esc_cg.CFRelease(ev)
    except Exception:
        _tlog('escape post failed', traceback.format_exc().replace('\n', ' | '))


def _escape_or_deselect():
    """Escape / Deselect — one key. A command running (its dialog, a sketch tool, a
    dimension mid-pick) gets a real Esc, so it means exactly what Esc means there.
    Nothing running: Deselect, the same one deselectAllCmd is — the dock's cursor AND
    the viewport's selection (the viewport's alone with the dock closed).

    Read inside the veto, so activeCommand is still the command that was running (a
    vetoed command never replaces it — the cycle keys rely on the same thing). Fusion's
    own text boxes (inline rename, Data Panel search) never fire shortcuts, so the key
    never reaches here from one; the dock's are escaped by the page itself."""
    try:
        running = ui.activeCommand or ''
    except Exception:
        running = ''
    try:
        idle = _hotkeys()._is_idle(running)
    except Exception:
        idle = running in ('', 'SelectCommand')
    _tlog('escape-or-deselect: activeCommand=%s idle=%s' % (running, idle))
    if not idle and running != ESCDESEL_CMD_ID:
        _post_escape()
        return
    if not _hand_to_window('deselectAllCmd'):
        try:
            ui.activeSelections.clear()
        except Exception:
            pass


# Super Escape's Esc presses still owed, and when the next may go (monotonic).
_super_esc = {'left': 0, 'next': 0.0}
_SUPER_ESC_MAX = 8          # a dropdown in a dialog in a command is 3; 8 is plenty
_SUPER_ESC_GAP = 0.12       # long enough for Fusion to act on the last one


def _super_escape():
    """Super Escape — everything open, out, in one press. Our windows close what they
    have up through their own cancels (the Value window's Close Whole Dialog; the dock
    page's rename, menus, cut), then Fusion gets real Esc presses, one per tick, until
    no command is running. Esc, never terminateActiveCommand — that COMMITS
    (Dimension Value relies on it), and an escape must cancel. Nothing running and
    nothing open: nothing happens (no Deselect — that is Escape / Deselect's)."""
    dock = getattr(builtins, '_fusion_dock', None)
    if (isinstance(dock, dict) and dock.get('mode') == 'timeline'
            and callable(dock.get('push'))):
        try:
            dock['push'](SUPERESC_CMD_ID)
        except Exception:
            pass
    elif _server_mod is not None and _server_mod.window_alive():
        _server_mod.push_ui_action(SUPERESC_CMD_ID)

    def value_window():                    # another add-in's server, so over HTTP
        try:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(
                'http://127.0.0.1:8734/escape', data=b'{}', method='POST',
                headers={'Content-Type': 'application/json'}), timeout=1).read()
        except Exception:
            pass
    threading.Thread(target=value_window, daemon=True).start()

    _super_esc['left'] = _SUPER_ESC_MAX
    _super_esc['next'] = 0.0
    _super_escape_step()


def _super_escape_step():
    """One owed Esc, if a command is still running (called from the veto, then every
    tick until idle or the presses run out)."""
    if _super_esc['left'] <= 0 or time.monotonic() < _super_esc['next']:
        return
    try:
        running = ui.activeCommand or ''
    except Exception:
        running = ''
    try:
        idle = _hotkeys()._is_idle(running)
    except Exception:
        idle = running in ('', 'SelectCommand')
    if idle or running == SUPERESC_CMD_ID:
        _super_esc['left'] = 0
        return
    _tlog('super escape: activeCommand=%s left=%d' % (running, _super_esc['left']))
    _super_esc['left'] -= 1
    _super_esc['next'] = time.monotonic() + _SUPER_ESC_GAP
    _post_escape()


def _jump_to_recent_sketch():
    """Enter the most recently edited sketch that is not the one open now."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        current = ''
        try:
            if fvs.is_editing_sketch(design):
                sk = fvs.recall_edit_sketch(design) or fvs.edited_sketch_from_marker(design)
                if sk:
                    current = fvs.leaf_key_for(sk) or ''
        except Exception:
            current = ''
        for key, token in list(_sketch_history):
            if key == current:
                continue
            ent = _find_entity(design, token)
            if ent is None:
                continue
            try:
                native = ent.nativeObject or ent
            except Exception:
                native = ent
            owner = _owning_occurrence_of(design, native)
            _enter_sketch_obj(design, native, owner)
            return
    except Exception:
        _tlog('recent sketch failed:', traceback.format_exc().replace('\n', ' | '))


def _owning_occurrence_of(design, sketch):
    """The occurrence a native sketch is reached through (None on the root). A sketch
    of a component that occurs more than once takes the first occurrence."""
    try:
        comp = sketch.parentComponent
        root = design.rootComponent
        if _same_component(comp, root):
            return None
        occs = root.allOccurrencesByComponent(comp)
        return occs.item(0) if occs.count else None
    except Exception:
        return None


def _hand_to_window(cid):
    """Give a browser-homed action to whichever page is listening; True if one took it.

    While the dock shows the vertical timeline instead of this window, the same keys
    drive THAT list: the Timeline add-in owns the mode and hands us its page's push
    (builtins._fusion_dock — see AddIns/Timeline). Falls through to this window when
    its page isn't there to take the key, and to nothing (False) with no window up.

    Called from the commandStarting veto below, and directly by hotkeys.press (set_direct
    in _register_forwarded_actions) for a chord a companion window caught — so that path
    never has to start a Fusion command just to have it vetoed."""
    if cid not in _browser_action_ids or _server_mod is None:
        return False
    dock = getattr(builtins, '_fusion_dock', None)
    if (isinstance(dock, dict) and dock.get('mode') == 'timeline'
            and callable(dock.get('push'))):
        if dock['push'](cid):
            return True
    if _server_mod.window_alive():
        _server_mod.push_ui_action(cid)
        return True
    return False


def _direct_press(cid):
    """hotkeys.press's way in (see _hand_to_window). A go-in on a selected sketch
    dimension is the Dimension Value add-in's — declined, so press falls back to
    executing the command and that add-in's own veto opens its dialog."""
    if cid in GOIN_CMD_IDS:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design and _selection_has_dimension(design):
            return False
    return _hand_to_window(cid)


class HotkeyStartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Veto our own hotkey commands at commandStarting and act on them here instead.

    Fusion is single-command-at-a-time: starting any command terminates the one
    already running, which is why pressing the browser hotkey mid-Extrude used
    to drop you out of Extrude. The toggle itself never needed a Fusion command
    at all — it is three lines of shell — so the command is refused before it
    can become the active one.

    Whether the previously active command SURVIVES depends on whether Fusion
    terminates it before or after firing commandStarting for the new one, which
    is not documented. The log records activeCommand around the veto so the
    ordering is visible rather than guessed at.
    """
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cid = args.commandId

            # Jumping to another level always tidies the tree, even on the way out of a
            # sketch. Observed, not intercepted: the command still runs as it always did.
            if cid in NAV_JUMP_CMD_IDS and _server_mod:
                try:
                    _server_mod.note_nav_jump()
                except Exception:
                    pass

            # Another action has this key in the domain we are actually in — the router
            # is about to run it. Stand aside: it will veto the command itself, and
            # forwarding as well would run BOTH (a go-in into the tree behind whatever
            # the key really meant). Same reasoning as the dimension case below: several
            # handlers see this event and their order is undefined, so each declines
            # independently.
            try:
                if _hotkeys().overridden(cid):
                    return
            except Exception:
                pass

            # A browser-homed action: the behaviour is in the window, so Fusion has
            # nothing to run. Veto and hand the id over; the window picks it up on its
            # next /state poll and runs exactly the JS the key runs when the window is
            # the focused surface.
            # Only hand it over while the window is actually listening. With the dock
            # closed there is nothing to run the action, and vetoing regardless would
            # silently break any of these ids that ALSO has real Fusion behaviour of its
            # own (Deselect All has a toolbar button). Falling through runs it natively.
            # "Go in" on a selected sketch dimension means EDIT ITS VALUE, and that is the
            # Dimension Value add-in's job — it vetoes the same command and opens its
            # dialog. Both add-ins watch commandStarting and the order they run in is not
            # defined, so this side has to decline independently: vetoing is idempotent,
            # but forwarding is not, and a forwarded go-in would enter the sketch behind
            # the dialog. With no dimension selected nothing here changes.
            if cid in GOIN_CMD_IDS:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if design and _selection_has_dimension(design):
                    return

            # ⌘X / ⌘V pressed with the VIEWPORT focused. Handed to the window only when
            # it says it is holding something: parameters selected for a cut, or a cut
            # pending for a paste. In every other case this falls straight through and
            # Fusion's own Cut and Paste behave exactly as they always did — including
            # inside Fusion's text fields, which never reach here at all (Fusion does
            # not fire shortcuts while one has focus, which is what makes the whole
            # single-letter keymap workable in the first place).
            #
            # Taking ⌘X costs Fusion nothing even in principle: a parameter cannot be
            # selected in the viewport, so whenever the browser's selection is
            # parameters, Fusion's own selection is empty and its Cut had nothing to do.
            # ⌘V does shadow Fusion's Paste for as long as a cut is pending — the cut
            # rows are drawn dimmed the whole time, and Esc clears it.
            if cid in (CUT_CMD_ID, PASTE_CMD_ID) and _server_mod and _server_mod.window_alive():
                cutting = cid == CUT_CMD_ID
                if _server_mod.ui_flags().get('canCut' if cutting else 'hasCut'):
                    args.isCanceled = True
                    _server_mod.push_ui_action(BROWSER_CUT_ID if cutting else BROWSER_PASTE_ID)
                return

            if _hand_to_window(cid):
                args.isCanceled = True
                return

            if cid in _browser_action_ids and _server_mod:
                twin = _WINDOW_ACTION_TWINS.get(cid)
                if twin:
                    # No window to run it: run the viewport's own version instead.
                    args.isCanceled = True
                    cd = ui.commandDefinitions.itemById(twin)
                    if cd:
                        cd.execute()
                    return

            if cid == SWAP_CMD_ID:
                args.isCanceled = True
                _swap_browsers()
                return

            if cid == RECENT_SKETCH_CMD_ID:
                args.isCanceled = True
                _jump_to_recent_sketch()
                return

            if cid == ESCDESEL_CMD_ID:
                args.isCanceled = True
                _escape_or_deselect()
                return

            if cid == SUPERESC_CMD_ID:
                args.isCanceled = True
                _super_escape()
                return

            if cid != CMD_ID:
                return
            try:
                before = ui.activeCommand
            except Exception:
                before = '?'
            args.isCanceled = True
            _toggle_browser()
            try:
                after = ui.activeCommand
            except Exception:
                after = '?'
            _tlog('dock toggle vetoed %s: activeCommand before=%s after=%s' % (
                CMD_ID, before, after))
        except Exception:
            _tlog('starting handler error:', traceback.format_exc().replace('\n', ' | '))


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Fallback only. HotkeyStartingHandler normally vetoes the command
        # before it gets this far, so reaching here means the veto did not take.
        _tlog('execute handler reached — veto did not take')
        _toggle_browser()


class ForwardedCreatedHandler(adsk.core.CommandCreatedEventHandler):
    """Fallback for a browser-homed action whose veto did not take."""
    def __init__(self, cmd_id):
        super().__init__()
        self.cmd_id = cmd_id

    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = ForwardedExecuteHandler(self.cmd_id)
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class ForwardedExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, cmd_id):
        super().__init__()
        self.cmd_id = cmd_id

    def notify(self, args):
        _tlog('forwarded action reached execute — veto did not take:', self.cmd_id)
        try:
            if _server_mod:
                _server_mod.push_ui_action(self.cmd_id)
        except Exception:
            pass


# ── Viewport-side rename / toggle-visibility (mirror the window's Cmd+R / Cmd+E) ──
def _selection_target(design):
    """The entity a viewport hotkey should act on: the current selection mapped to its
    body / sketch / occurrence (geometry → its owning body, sketch curve → its sketch),
    else the active component when nothing usable is selected."""
    try:
        sel = ui.activeSelections
        if sel.count:
            ent = sel.item(sel.count - 1).entity
            if isinstance(ent, (adsk.fusion.BRepFace, adsk.fusion.BRepEdge, adsk.fusion.BRepVertex)):
                ent = ent.body
            if isinstance(ent, (adsk.fusion.Occurrence, adsk.fusion.BRepBody, adsk.fusion.Sketch)):
                return ent
            ps = getattr(ent, 'parentSketch', None)
            if ps:
                return ps
            if ent:
                return ent
    except Exception:
        pass
    try:
        return design.activeComponent
    except Exception:
        return None


def _current_name(ent):
    try:
        if isinstance(ent, adsk.fusion.Occurrence):
            return ent.component.name
        return ent.name
    except Exception:
        return ''


_pending_rename_ent = None


class RenameCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _pending_rename_ent
        try:
            if self not in handlers:
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            _pending_rename_ent = _selection_target(design) if design else None
            cmd = args.command
            cmd.commandInputs.addStringValueInput('newname', 'New name', _current_name(_pending_rename_ent))
            ex = RenameExecuteHandler()
            cmd.execute.add(ex)
            handlers.append(ex)
        except Exception:
            pass


class RenameExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _pending_rename_ent
        try:
            inp = args.command.commandInputs.itemById('newname')
            name = (inp.value if inp else '').strip()
            ent = _pending_rename_ent
            if ent and name:
                if isinstance(ent, adsk.fusion.Occurrence):
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    _rename_component(design, ent.component, name)
                elif isinstance(ent, adsk.fusion.Component):
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    _rename_component(design, ent, name)
                else:
                    ent.name = name
        except Exception:
            pass
        finally:
            _pending_rename_ent = None


class EnterSketchCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            ex = EnterSketchExecuteHandler()
            args.command.execute.add(ex)
            handlers.append(ex)
        except Exception:
            pass


class EnterSketchExecuteHandler(adsk.core.CommandEventHandler):
    """Enter the selected sketch. This is the ONLY way the viewport opens a sketch now
    — clicking one just selects it — so bind this to Return."""
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design or fvs.is_editing_sketch(design):
                return
            sk, owner = _sketch_from_selection()
            if sk is not None:
                _enter_sketch_obj(design, sk, owner)
        except Exception:
            pass


class ToggleVisCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            ex = ToggleVisExecuteHandler()
            args.command.execute.add(ex)
            handlers.append(ex)
        except Exception:
            pass


class ToggleVisExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            ent = _selection_target(design) if design else None
            if not ent:
                return
            if isinstance(ent, adsk.fusion.Occurrence):
                ent.isLightBulbOn = not ent.isLightBulbOn
                fvs.note_occ_intent(ent, ent.isLightBulbOn)
            elif hasattr(ent, 'isVisible'):
                ent.isVisible = not ent.isVisible
        except Exception:
            pass
