"""
Fusion Params — add-in.

Headless backend: no window of its own any more (the Browser add-in shows
parameters in its own sketches/parameters toggle instead). Starts
automatically on Fusion launch (runOnStartup: true). Runs a local HTTP server
in a background thread and syncs user parameters every 500 ms via a custom
event (so all Fusion API calls land on the main thread). Link Driven Dimension
moved to the Dimension Value add-in (its value box names the link with the
structured convention's stepper); this add-in still keeps each link's pair of names
in step on rename and re-points one at another dimension (_relink_to), through the
shared Driven Link.py.

Threading model:
  - Background poller thread fires EVENT_ID every 500 ms, skipping a tick while
    the previous one is still queued (no backlog behind a busy main thread).
  - SyncHandler.notify() runs on the Fusion main thread:
      1. Apply any expression changes queued by the browser.
      2. Read all userParameters and push a snapshot to the server. Dependants
         come from a cache rebuilt only when something may have changed — see
         _deps_cache; per-tick dependentParameters caused Fusion's beachballs.
  - The HTTP server thread only touches the in-memory snapshot (under a lock)
    and the cmd queue — never the Fusion API.

Callers other than this add-in's own window: the BROWSER window's page (served
from 8732) posts parameter edits here — inline value edits, renames, deletes —
because parameters are owned HERE and it refuses to duplicate that. Those are
cross-origin, so server.py answers the CORS preflight; without do_OPTIONS the
preflight gets a 501 and the real request is never sent at all, which looks
exactly like an edit that silently does not stick.

NOTE on the `self not in handlers` guard used elsewhere: Fusion's Python
binding may reconstruct a new wrapper for `self` on each CustomEvent
notification, making identity comparison unreliable. We use a module-level
`_active` flag instead.
"""

import adsk.core, adsk.fusion
import builtins, importlib.util, os, re, threading, time, traceback
import types as _types

# The process-wide "someone is already in the model" latch. This add-in is the one
# that MUTATES — renames, expressions, deletes, doc.save() — and every one of those
# recomputes, during which Fusion pumps the task queue that delivers the other
# add-ins' ticks. Holding the latch across the tick is what keeps them out of a
# half-rebuilt model. Read Tick Guard.py's header before changing this.
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
tickguard = _load_local('tickguard', 'Tick Guard.py')
# A driven-dimension link's naming (the alias / case-flipped reference pair), shared
# with Dimension Value, which makes the links.
drivenlink = _load_local('drivenlink', 'Driven Link.py')
# Typed numbers as hidden number parameters (read its header): never listed here, and
# the number inside an edited expression ("Shelf + 10") is made one.
numbers = _load_local('numbers', 'Number Params.py')

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

EVENT_ID = 'FusionParamsSyncEvent'

# Title for the message boxes a re-pointed link (_relink_to) can raise.
LINK_TITLE = 'Link Driven Dimension'

EDITOR_DIR = '/Users/josh/Code/Scripts/fusion_params'
SERVER_PY  = os.path.join(EDITOR_DIR, 'server.py')

_httpd      = None
_server_mod = None
_stop_evt   = threading.Event()
_custom_evt = None
_active     = False   # True between run() and stop(); guards SyncHandler

# A driven row in the window is waiting to be pointed at a dimension the user clicks.
# {'name': alias parameter, 'until': monotonic deadline}. Polled in _sync rather than
# hung off activeSelectionChanged: _sync already runs on the main thread every 500 ms,
# which is quick enough for a click and adds no new event handler to keep alive.
_relink = {'name': '', 'until': 0.0}
_relink_trace = []    # last dozen arm/disarm events, surfaced at /debug
RELINK_TIMEOUT = 90.0   # generous: Escape, Enter again, any key, or picking
                       # something else all cancel, so the deadline is a backstop

# Dependant names per user parameter, for the browser's delete decisions. Reading
# p.dependentParameters for every parameter on every tick was the cause of Fusion's
# beachballs (Sep 2026): with ~180 parameters it pinned the main thread at ~60% CPU
# (each call walks the whole design's configuration overrides). So it is cached and
# rebuilt only when something may have changed: a command ended (edits, undo, redo),
# the document switched, the parameter list changed, or we applied a browser command.
_deps_cache = {}
_where_cache = {}     # model-parameter name → "Component / Sketch" it lives in (built with _deps_cache)
_kind_cache = {}      # model-parameter name → 'sketch' | 'feature' — what it belongs to
_key_cache = {}       # model-parameter name → its sketch's Browser row key (see _sketch_key)
_deps_dirty = True
_deps_key   = None    # (doc_id, tuple of user-parameter names) the cache was built for
_param_objs     = []   # the UserParameter objects last fetched — see _sync's snapshot
_param_objs_key = None # (doc_id, count) they were fetched for
_param_objs_at  = 0.0
# Commands that cannot change a parameter or its dependants: ending one must not
# trigger the dependants rebuild (see LinkTerminatedHandler).
_NO_PARAM_CMDS = frozenset({
    'SelectCommand', 'SketchActivate',
    'ActivateAndIsolateCmd', 'GoToParentComponentCmd', 'UnisolateTopLevelCmd',
    'GoToBottomLevelCmd', 'BrowserEnterSketchSelectionCmd',
    'ConstrainedOrbitCommand', 'FreeOrbitCommand', 'PanCommand', 'ZoomCommand',
    'SetOrbitCenterCommand', 'ResetOrbitCenterCommand', 'FitCommand',
})
_tick_pending = 0.0   # monotonic time the poller last fired an unhandled tick; 0 = none
DEPS_IDLE_S = 0.8     # no keyboard/mouse input this long before the dependants walk
_cg_idle = []


def _input_idle():
    """Seconds since the last keyboard/mouse/trackpad input (HID state, in-process —
    Section Sketch's rule). Large if CoreGraphics can't be reached."""
    try:
        if not _cg_idle:
            import ctypes
            cg = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            f = cg.CGEventSourceSecondsSinceLastEventType
            f.restype = ctypes.c_double
            f.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            _cg_idle.append(f)
        return _cg_idle[0](1, 0xFFFFFFFF)
    except Exception:
        return 1e9


# ── Server helpers ─────────────────────────────────────────────────────────────

def _load_server():
    global _server_mod
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('fusion_params_server', SERVER_PY)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _server_mod = mod
    return _server_mod


def _start_server():
    global _httpd
    mod = _load_server()
    if _httpd is None:
        try:
            _httpd = mod.make_server()
            threading.Thread(target=_httpd.serve_forever, daemon=True).start()
        except OSError:
            _httpd = None  # port already taken — another instance is serving


def _stop_server():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None


# ── Background poller ──────────────────────────────────────────────────────────

def _poller():
    """Fire the custom event every 500 ms from the background thread — but never
    while the previous one is still queued. Unconditional firing let ticks pile up
    behind a busy main thread and drain as one long beachball. The 5 s staleness
    reset is a backstop so a lost notify can't stop the poller for good."""
    global _tick_pending
    while not _stop_evt.is_set():
        now = time.monotonic()
        if not _tick_pending or now - _tick_pending > 5.0:
            _tick_pending = now
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                _tick_pending = 0.0
        _stop_evt.wait(0.5)


# ── Main-thread sync ───────────────────────────────────────────────────────────

def _delete_param(design, name, replacement):
    """Delete user parameter `name`. If `replacement` is given, first rewrite every
    dependent parameter's expression to reference `replacement` instead of `name`
    (so sketch dimensions / feature inputs / other params keep working), then
    delete. Without a replacement, deleteMe() simply fails if anything still uses
    it — the browser surfaces that and rolls back its optimistic removal."""
    p = design.userParameters.itemByName(name)
    if not p:
        return
    if replacement:
        # `replacement` may be a bare parameter name or an arbitrary expression
        # (e.g. "Width/2"). Wrap anything that isn't a single identifier in
        # parentheses so it slots into the dependents' expressions safely, and use
        # a function replacement so regex backreferences aren't interpreted.
        repl = replacement if re.match(r'^[A-Za-z_]\w*$', replacement) else '(' + replacement + ')'
        token = re.compile(r'\b' + re.escape(name) + r'\b')
        deps = p.dependentParameters
        for i in range(deps.count):
            dp = deps.item(i)
            try:
                dp.expression = token.sub(lambda _m: repl, dp.expression)
            except Exception:
                # A dependent we can't rewrite (read-only / driven) — leave it; the
                # deleteMe() below will then fail and the UI will report it.
                _server_mod and _server_mod.set_debug(last_error=traceback.format_exc())
    p.deleteMe()


def _driven_ref_names(design):
    """Names of every driven (reference) dimension's own parameter. A user parameter
    whose expression is exactly one of these IS the window's view of that dimension."""
    out = set()
    try:
        for ci in range(design.allComponents.count):
            comp = design.allComponents.item(ci)
            for si in range(comp.sketches.count):
                dims = comp.sketches.item(si).sketchDimensions
                for di in range(dims.count):
                    d = dims.item(di)
                    if not d.isDriving:
                        out.add(d.parameter.name)
    except Exception:
        pass
    return out


# _driven_ref_names walks every sketch dimension in the design: ~5 ms a tick on 260612
# (measured 2026-09-29), half the whole tick. Its answer only moves when a dimension is
# added, removed, renamed or flipped driving/driven — each of which ends a command or
# moves the timeline — so it is kept until one of those happens. The user-parameter
# count is in the key too: Dimension Value links a driven dimension from its own tick,
# outside any command, renaming the dimension and adding the alias in one go.
_refs_cache = {'key': None, 'names': set()}
_refs_dirty = True    # set with _deps_dirty: a command ended, or the window edited


def _driven_refs_cached(design):
    global _refs_dirty
    try:
        doc = app.activeDocument
        key = ((doc.name if doc else '', design.userParameters.count)
               + tuple(tickguard.timeline_pos(design)))
    except Exception:
        key = None                                # can't tell -> don't trust the cache
    c = _refs_cache
    if _refs_dirty or key is None or key != c['key']:
        _refs_dirty = False
        c['names'] = _driven_ref_names(design)
        c['key'] = key
    return set(c['names'])


def _sync(design):
    global _deps_dirty, _deps_key, _refs_dirty
    mod = _server_mod
    if mod is None:
        return

    # Both the command loop and the snapshot below need it.
    driven_refs = _driven_refs_cached(design)

    # Apply changes the browser posted: add a new parameter, rename, or edit
    # expression/comment. Each is independent so one bad command can't block others.
    cmds = mod.pop_commands()
    if cmds:
        _deps_dirty = True
        _refs_dirty = True
    for cmd in cmds:
        try:
            if cmd.get('action') == 'save':
                doc = app.activeDocument
                if doc:
                    doc.save('')
                continue
            if cmd.get('action') == 'add':
                expr, _left, made = numbers.convert(design, cmd['expression'],
                                                    cmd.get('unit', ''), keep_whole=True)
                try:
                    design.userParameters.add(cmd['name'],
                                              adsk.core.ValueInput.createByString(expr),
                                              cmd.get('unit', ''), cmd.get('comment', ''))
                except Exception:
                    numbers.drop(design, made)
                    raise
                continue
            if cmd.get('action') == 'delete':
                _delete_param(design, cmd['name'], cmd.get('replacement'))
                continue
            if cmd.get('action') == 'command':
                cd = ui.commandDefinitions.itemById(cmd.get('cmd_id', ''))
                if cd:
                    cd.execute()
                continue
            if cmd.get('action') == 'relink':
                # An empty name is the window cancelling.
                _trace(mod, 'window: %s %r (%r) while armed=%r sel=%d' % (
                    'arm' if cmd.get('name') else 'cancel', cmd.get('name', ''),
                    cmd.get('why', ''), _relink['name'], _selection_count()))
                _relink['name'] = cmd.get('name', '')
                _relink['until'] = time.monotonic() + RELINK_TIMEOUT
                # Drop whatever is already selected in the viewport as we arm. Without
                # this, a dimension left selected from earlier is taken as the pick on
                # the very next tick — the mode arms and disarms between two polls, so
                # the window never shows "(click a dimension…)" and Enter looks dead.
                # Only a selection made AFTER arming should count as the pick.
                if _relink['name']:
                    try:
                        ui.activeSelections.clear()
                    except Exception:
                        pass
                mod.set_relink_armed(_relink['name'])
                continue
            # A driven dimension's parameter is a ModelParameter, not a user parameter,
            # so renames and comments have to reach allParameters as well.
            up = design.userParameters.itemByName(cmd['name'])
            p  = up or design.allParameters.itemByName(cmd['name'])
            if not p:
                continue
            if 'newName' in cmd:
                want = _squash(cmd['newName'])    # the window strips too; backstop here
                # Renaming a driven-dimension alias has to rename the dimension's own
                # parameter too, or hovering the dimension would keep showing the old
                # name. Do it FIRST: renaming a parameter rewrites its dependents'
                # expressions automatically, so the alias follows without our help.
                if up and (up.expression or '').strip() in driven_refs:
                    ref = design.allParameters.itemByName(up.expression.strip())
                    if ref:
                        ref.name = _unique_name(design, _flip_case(want))
                p.name = want
                if p.name != want:                # silently ignored — see Driven Link.py
                    mod.set_debug(last_error='Fusion would not rename %r to %r'
                                             % (cmd['name'], want))
            if 'expression' in cmd and up:        # a measured value has no editable expression
                why = numbers.set_expression(design, up, cmd['expression'], keep_whole=True)
                if why:
                    raise RuntimeError(why)
            if 'comment' in cmd:
                p.comment = cmd['comment']
        except Exception:
            # Bad expression / duplicate name / invalid unit — report, keep going.
            mod.set_debug(last_error=traceback.format_exc())

    # A driven row is waiting to be pointed at a dimension. Take the first driven
    # dimension the user selects and disarm. Selecting anything else is ignored rather
    # than treated as a cancel, so a stray click mid-aim doesn't drop the mode — the
    # deadline and the window's Escape are the two ways out.
    if _relink['name']:
        if time.monotonic() > _relink['until']:
            _trace(mod, 'timeout: %s' % _relink['name'])
            _relink['name'] = ''
            mod.set_relink_armed('')
        else:
            try:
                sel = ui.activeSelections
                ents = [sel.item(i).entity for i in range(sel.count)]
            except Exception:
                ents = []
            picked = _driven_dimension(ents)
            if not picked and ents:
                _trace(mod, 'cancelled by selection: %s (%d entities, none driven)'
                            % (_relink['name'], len(ents)))
                # Selected something that isn't a driven dimension — treat it as a change
                # of mind and drop the mode. Clicking EMPTY space can't be caught here:
                # it clears the selection, which is indistinguishable from not having
                # picked yet. The window's any-key cancel covers that case instead.
                _relink['name'] = ''
                mod.set_relink_armed('')
            elif picked:
                _trace(mod, 'picked %s for %s' % (picked.parameter.name, _relink['name']))
                alias, _relink['name'] = _relink['name'], ''
                mod.set_relink_armed('')
                try:
                    err = _relink_to(design, alias, picked, driven_refs)
                except Exception:
                    err = traceback.format_exc()
                try:
                    ui.activeSelections.clear()
                except Exception:
                    pass
                _refs_dirty = True
                driven_refs = _driven_refs_cached(design)   # the names just moved around
                _deps_dirty = True
                if err:
                    ui.messageBox(err, LINK_TITLE)

    # Identify the active document so the browser stores a separate group tree per
    # file. dataFile.id is stable for a saved file; fall back to the name (or a
    # sentinel) for never-saved documents.
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

    # Snapshot all user parameters for the browser. UserParameters.item(i) costs
    # ~0.15 ms EACH (25 ms for 161, every 500 ms tick — measured 2026-09-26), while
    # reading name/expression off an object already held is ~free. So the objects are
    # re-fetched only when something could have changed the set: a command ended
    # (_deps_dirty, which also covers the window's own edits), the count or document
    # changed, or one went invalid. No time backstop: with Number Params' hidden ones
    # there are 500+, and the 5 s re-fetch was a 0.2–0.7 s hitch every 5 s (260612,
    # 2026-10-07); a rename keeps the object, and names are read fresh off it below.
    global _param_objs, _param_objs_key, _param_objs_at
    params = []
    try:
        ups = design.userParameters
        pkey = (doc_id, ups.count)
        now = time.monotonic()
        # Not on every command end (_deps_dirty): the objects outlive edits, renames and
        # recomputes, and the re-fetch was 133 ms for 534 after each one — every line
        # drawn in a sketch (2026-10-08). A delete/add shows in the count or isValid.
        stale = pkey != _param_objs_key
        if not stale:
            try:
                stale = not all(p.isValid for p in _param_objs)
            except Exception:
                stale = True
        if stale:
            for i in range(ups.count):
                try:
                    params.append(ups.item(i))
                except Exception:
                    pass
            _param_objs, _param_objs_key, _param_objs_at = params, pkey, now
        else:
            params = list(_param_objs)
    except Exception:
        pass

    names = tuple(p.name for p in params)
    key = (doc_id, names)
    if key != _deps_key:
        _deps_dirty = True
    # The dependants walk (~150 ms idle, 1.5 s just after a sketch exit) waits for a
    # pause in input: it was a hitch after every tool in a sketch. The rows keep the
    # last walk's counts meanwhile.
    if _deps_dirty and _input_idle() >= DEPS_IDLE_S:
        # Clear first: a command ending mid-rebuild sets it again for the next tick.
        _deps_dirty = False
        _deps_key = key
        _deps_cache.clear()
        _where_cache.clear()
        _kind_cache.clear()
        _key_cache.clear()
        for p in params:
            # Names as well as the count: the browser deletes a multi-selection in one
            # go when every dependant is itself in the selection, and it can only tell
            # that by name. Model parameters (d12 …) show up here too and never match
            # a user parameter, which is exactly right — they block the delete.
            if numbers.is_number_name(p.name):
                continue                  # never a row (below); hundreds after the sweep
            try:
                dp = p.dependentParameters
                deps = [dp.item(j) for j in range(dp.count)]
                _deps_cache[p.name] = [d.name for d in deps]
                for d in deps:
                    if d.name not in _where_cache:
                        w, k, key = _model_param_where(d)
                        if w:
                            _where_cache[d.name] = w
                            _kind_cache[d.name] = k
                        if key:
                            _key_cache[d.name] = key
            except Exception:
                _deps_cache[p.name] = []

    # Typed numbers: never a row of their own, and written back as their literals in
    # every expression that names one (editing that text back keeps them — Number Params).
    nums = {}
    for p in params:
        try:
            if numbers.is_number_name(p.name):
                nums[p.name] = p.expression
        except Exception:
            pass
    rows = []
    for p in params:
        try:
            if p.name in nums:
                continue
            dep_names = _deps_cache.get(p.name, [])
            row = {
                'name':       p.name,
                'expression': numbers.np.display(p.expression, nums) if nums else p.expression,
                'unit':       p.unit or '',
                'comment':    p.comment,
                'deps':       len(dep_names),
                'depNames':   dep_names,
                # Where each model-parameter dependant lives, for the delete dialog:
                # "Component / Sketch" (or "Component / Feature" for a feature's own).
                'depWhere':   {n: _where_cache[n] for n in dep_names if n in _where_cache},
                # …and whether that is a sketch or a feature, which colours it there.
                'depKind':    {n: _kind_cache[n] for n in dep_names if n in _kind_cache},
                # …and a sketch's row key, so the dialog's Go To can find its row.
                'depKey':     {n: _key_cache[n] for n in dep_names if n in _key_cache},
            }
            # An alias for a driven dimension shows the MEASUREMENT rather than the
            # reference it is literally made of — "(266.15mm)" is the useful thing to
            # see, not "frontMitrePositionY". p.expression would also carry it at full
            # precision, and formatInternalValue ignores the precision preference
            # (it gave '266.1457037 mm' against a generalPrecision of 2), so convert
            # and round against the preference directly.
            if (p.expression or '').strip() in driven_refs:
                row['kind'] = 'driven'
                try:
                    um = design.unitsManager
                    row['expression'] = '%.*f %s' % (
                        _precision(), um.convert(p.value, um.internalUnits, p.unit), p.unit)
                except Exception:
                    pass
            rows.append(row)
        except Exception:
            pass

    mod.update_params(rows, doc_id)


# ── Driven-dimension links ─────────────────────────────────────────────────────
# Made by Dimension Value (see Driven Link.py); kept in step and re-pointed here.

_unique_name = drivenlink.unique_name
_flip_case = drivenlink.flip_case
_driven_dimension = drivenlink.driven_dimension


def _squash(name):
    """A typed name with its whitespace removed: Fusion silently ignores a parameter
    name holding a space, so "Boot Gap" is taken as the "BootGap" that was meant."""
    return re.sub(r'\s+', '', name or '')


def _precision():
    """Decimal places Fusion is set to display values at (Preferences > Units)."""
    try:
        return int(app.preferences.unitAndValuePreferences.generalPrecision)
    except Exception:
        return 2


def _selection_count():
    try:
        return ui.activeSelections.count
    except Exception:
        return -1


def _next_auto_name(design):
    """An unused Fusion-style dimension name (d1, d2…). A dimension's parameter can be
    renamed but never deleted, so this is how an un-linked dimension is handed back
    something that looks untouched instead of keeping a name it no longer owns."""
    top = 0
    for i in range(design.allParameters.count):
        m = re.match(r'^d(\d+)$', design.allParameters.item(i).name)
        if m:
            top = max(top, int(m.group(1)))
    return 'd%d' % (top + 1)


def _trace(mod, msg):
    """Record how the viewport-pick mode started and ended, on the existing /debug
    channel. Permanent and deliberately small: this mode is driven from two processes
    and a 500 ms poll, so "it just didn't arm" is otherwise unfalsifiable."""
    try:
        line = time.strftime('%H:%M:%S ') + msg
        _relink_trace.append(line)
        del _relink_trace[:-12]
        mod.set_debug(relink_trace=list(_relink_trace))
    except Exception:
        pass


def _sketch_timeline_index(design, sketch):
    """Where a sketch sits in the timeline, or None."""
    try:
        tok = sketch.entityToken
        tl = design.timeline
        for i in range(tl.count):
            try:
                e = tl.item(i).entity
                if e and e.entityToken == tok:
                    return i
            except Exception:
                pass
    except Exception:
        pass
    return None


def _consumer_limit(design, up):
    """The earliest timeline index at which anything driven by `up` is built, plus the
    dimension and sketch that sit there. A parameter may only measure geometry from
    BEFORE this point. (None, '', '') if nothing consumes it, in which case any
    dimension is fair game."""
    best = (None, '', '')
    deps = up.dependentParameters
    for i in range(deps.count):
        d = deps.item(i)
        for ci in range(design.allComponents.count):
            c = design.allComponents.item(ci)
            for si in range(c.sketches.count):
                sk = c.sketches.item(si)
                for k in range(sk.sketchDimensions.count):
                    if sk.sketchDimensions.item(k).parameter.name != d.name:
                        continue
                    at = _sketch_timeline_index(design, sk)
                    if at is not None and (best[0] is None or at < best[0]):
                        best = (at, d.name, sk.name)
    return best


def _model_param_where(prm):
    """'Component / Sketch' for a sketch dimension's parameter, 'Component / Feature'
    for a feature's own (an extrude distance …), '' for a user parameter or anything
    unresolvable — with its kind, 'sketch' or 'feature', and a sketch's Browser row key
    ('' otherwise). Only runs when the deps cache is rebuilt."""
    mp = adsk.fusion.ModelParameter.cast(prm)
    if not mp:
        return '', '', ''
    try:
        comp = mp.component
        cb = mp.createdBy
        dim = adsk.fusion.SketchDimension.cast(cb)
        if dim:
            sk = dim.parentSketch
            return ('%s / %s' % (_comp_label(sk.parentComponent), sk.name), 'sketch',
                    _sketch_key(sk))
        # A sketch dimension's parameter usually reports the Sketch itself as createdBy,
        # and mp.component is then the ROOT whatever component the sketch is in — so the
        # creator's own component wins (features have one too).
        comp = getattr(cb, 'parentComponent', None) or comp
        name = getattr(cb, 'name', '') if cb else ''
        cname = _comp_label(comp) if comp else ''
        sk = adsk.fusion.Sketch.cast(cb)
        return ((('%s / %s' % (cname, name)) if name else cname),
                'sketch' if sk else 'feature', _sketch_key(sk) if sk else '')
    except Exception:
        return '', '', ''


def _sketch_key(sk):
    """The Browser row's key for a sketch — Visibility Snapshot.py's _leaf_key spelling,
    '<component id>|sk|<name>', a repeat of a name in the same component '…#n' by its
    place among the same-named."""
    try:
        comp, name = sk.parentComponent, sk.name
        key = '%s|sk|%s' % (comp.id, name)
        nth = 0
        for other in comp.sketches:
            if other.name != name:
                continue
            nth += 1
            if other == sk:
                break
        return key if nth <= 1 else '%s#%d' % (key, nth)
    except Exception:
        return ''


def _comp_label(comp):
    """A component as the Browser's row reads: 'Top Level' for the root (its real name
    is the document's, and it has no row), else the name without Fusion's dedup / instance
    suffix — the same stripping as Browser.py's _display_name."""
    try:
        if comp == comp.parentDesign.rootComponent:
            return 'Top Level'
    except Exception:
        pass
    name = comp.name or ''
    s = re.sub(r'\s*\(\d+\)\s*$', '', name)
    s = re.sub(r':\d+$', '', s)
    s = re.sub(r'\s+\d+$', '', s)
    return s.strip() or name


def _order_conflict(design, up, dim):
    """Why Fusion refused, when the reason is timeline order — '' if it isn't.

    A parameter that DRIVES something may only measure geometry built before that
    something, or the model would depend on a measurement taken later. Verified to be
    structural rather than a matter of what you do first: linking the parameter and only
    then pointing it at earlier geometry is refused just the same. Fusion reports it with
    the same bare "Expression is invalid" it uses for a name that doesn't exist, so
    without this the refusal is unreadable. Only runs on failure.
    """
    tgt = _sketch_timeline_index(design, dim.parentSketch)
    limit, dep_name, dep_sketch = _consumer_limit(design, up)
    if tgt is None or limit is None or limit > tgt:
        return ''

    msg = ('"%s" drives %s in "%s", which Fusion builds before "%s". Measuring that '
           'dimension would make the earlier sketch depend on a later one, so Fusion '
           'refuses it.' % (up.name, dep_name, dep_sketch, dim.parentSketch.name))

    # Turn the dead end into a choice: every driven dimension early enough to qualify.
    free, taken = [], []
    owners = {}
    for i in range(design.userParameters.count):
        u = design.userParameters.item(i)
        owners[(u.expression or '').strip()] = u.name
    for ci in range(design.allComponents.count):
        c = design.allComponents.item(ci)
        for si in range(c.sketches.count):
            sk = c.sketches.item(si)
            at = _sketch_timeline_index(design, sk)
            if at is None or at >= limit:
                continue
            for k in range(sk.sketchDimensions.count):
                dm = sk.sketchDimensions.item(k)
                if dm.isDriving:
                    continue
                owner = owners.get(dm.parameter.name)
                entry = '%s in "%s"' % (dm.parameter.name, sk.name)
                (taken if owner else free).append(
                    entry + (' (already linked to %s)' % owner if owner else ''))

    if free:
        msg += '\n\nDimensions early enough to use instead:\n  ' + '\n  '.join(free)
    elif taken:
        msg += ('\n\nNothing early enough is free — these are early enough but already '
                'linked:\n  ' + '\n  '.join(taken))
    else:
        msg += ('\n\nThere is no driven dimension early enough. Either add one in a '
                'sketch built before "%s", or drag "%s" earlier in the timeline.'
                % (dep_sketch, dim.parentSketch.name))
    return msg


def _relink_to(design, alias_name, dim, driven_refs):
    """Point an existing alias at a different driven dimension. '' on success.

    Every rename is undone if any step fails. Without that, a refused expression left
    the old dimension holding a neutral name and the new one holding the alias's name
    while the alias still pointed at the old — a tangle that got worse on each retry.
    """
    up = design.userParameters.itemByName(alias_name)
    if not up:
        return 'The parameter "%s" no longer exists.' % alias_name
    dim = dim.nativeObject or dim        # a pick inside a component comes back as a proxy
    new_ref = dim.parameter
    old_ref_name = (up.expression or '').strip()
    new_ref_name = new_ref.name
    if new_ref_name == old_ref_name:
        return ''                                   # already pointed here — nothing to do

    # Refuse to steal a dimension another parameter already owns; silently re-pointing
    # it would leave two rows claiming the same dimension.
    for i in range(design.userParameters.count):
        other = design.userParameters.item(i)
        if other.name != alias_name and (other.expression or '').strip() == new_ref_name:
            return 'That dimension is already linked to "%s".' % other.name

    # Catch a kind mismatch before touching anything. Fusion reports it as the same
    # opaque "Expression is invalid" you get for a name that doesn't exist, so an angular
    # dimension picked for a length parameter would otherwise be a mystery.
    try:
        design.unitsManager.convert(1.0, new_ref.unit or '', up.unit or '')
    except Exception:
        return ('That dimension measures %s and "%s" is in %s — pick a dimension of the '
                'same kind.' % (new_ref.unit or 'no units', alias_name, up.unit or 'no units'))

    # Do the part Fusion can refuse FIRST, using the dimension's CURRENT name. If the
    # reference isn't allowed, nothing has been renamed yet and there is nothing to undo
    # — which is the whole reason this is no longer done last.
    old_expr = up.expression
    try:
        up.expression = new_ref_name
    except Exception as exc:
        return ('Could not link "%s" to that dimension.\n\n%s\n\nNothing was changed.'
                % (alias_name, _order_conflict(design, up, dim) or str(exc)))

    renamed = []                                    # (current, original), newest last
    try:
        # Now tidy the names. The alias already points at the new dimension, so renaming
        # frees the old name and Fusion rewrites the alias's expression to follow.
        if old_ref_name in driven_refs:
            old = design.allParameters.itemByName(old_ref_name)
            if old:
                auto = _unique_name(design, _next_auto_name(design))
                old.name = auto
                renamed.append((auto, old_ref_name))

        want = _unique_name(design, _flip_case(alias_name))
        new_ref.name = want
        if new_ref.name != want:                    # silently ignored — see Driven Link.py
            raise RuntimeError('Fusion would not name that dimension\'s parameter "%s".'
                               % want)
        renamed.append((want, new_ref_name))
        return ''
    except Exception as exc:
        for cur, orig in reversed(renamed):          # newest undone first
            back = design.allParameters.itemByName(cur)
            if back:
                try:
                    back.name = orig
                except Exception:
                    pass
        alias = design.userParameters.itemByName(alias_name)
        if alias and old_expr:
            try:
                alias.expression = old_expr
            except Exception:
                pass
        return 'Could not link "%s" to that dimension — %s\n\nNothing was changed.' % (
            alias_name, str(exc) or exc.__class__.__name__)


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def run(context):
    global _custom_evt, _stop_evt, _active
    try:
        _start_server()

        _active = True
        _stop_evt.clear()
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = SyncHandler()
        _custom_evt.add(h)
        handlers.append(h)

        threading.Thread(target=_poller, daemon=True).start()

        ht = TerminatedHandler()
        ui.commandTerminated.add(ht)
        handlers.append(ht)

        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Fusion Params failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _stop_evt, _custom_evt, _active
    try:
        _active = False
        _stop_evt.set()
        handlers.clear()
        _stop_server()
        if _custom_evt:
            try:
                app.unregisterCustomEvent(EVENT_ID)
            except Exception:
                pass
            _custom_evt = None
        # Nothing on the toolbar to take down: Link Driven Dimension's control is
        # Dimension Value's now, under the same command id.
    except Exception:
        pass


# ── Event handlers ─────────────────────────────────────────────────────────────

class SyncHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        # Use module-level flag rather than identity check — Fusion's Python
        # binding may reconstruct the wrapper object on each notify() call,
        # making `self in handlers` unreliable for CustomEventHandler.
        global _tick_pending
        _tick_pending = 0.0
        if not _active:
            return
        # Stand down if anything else is already in the model, and keep everyone
        # else out of it while this tick's renames and expression edits recompute.
        # See Tick Guard.py. Missing one 500 ms frame costs nothing.
        with tickguard.tick('parameters') as go:
            if not go:
                return
            mod = _server_mod
            if mod:
                mod.bump_notify()
            try:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if mod:
                    mod.set_debug(design_found=bool(design))
                if design:
                    _sync(design)
                elif mod:
                    # Push an empty (but timestamped) snapshot so the UI shows
                    # "no user parameters" rather than hanging on "waiting".
                    mod.update_params([])
            except Exception:
                if mod:
                    mod.set_debug(last_error=traceback.format_exc())


class TerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _deps_dirty, _refs_dirty
        # Any command (edit, undo, redo) may have moved dependants — except the ones
        # that only select, navigate or open a sketch. Those end constantly, and each
        # dirty rebuild walks every parameter's dependants (~100 ms, measured
        # 2026-09-26), right in the middle of a sketch enter/exit.
        try:
            if args.commandId == 'SectionDoCmd' and \
                    builtins.__dict__.get('_section_do_curves_only'):
                return             # Section Sketch drawing its reference curves, nothing else
            if args.commandId not in _NO_PARAM_CMDS:
                _deps_dirty = _refs_dirty = True
        except Exception:
            _deps_dirty = _refs_dirty = True
