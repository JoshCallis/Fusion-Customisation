"""
Fusion Timeline — add-in.

Feeds the vertical timeline: the second panel of the left dock (FusionDock.app),
swapped with the Browser by DockTimelineToggleCmd. Serves
/Users/josh/Code/Scripts/fusion_timeline/server.py (port 8736) and, from a main-thread
custom-event tick, pushes the timeline rows to it and applies what the page posted
(rename, edit, suppress, toggle). The Browser add-in's skeleton, cut down.

TIMELINE MODE (builtins._fusion_dock — one dict per Fusion process, like Tick Guard's
latch, so the Browser add-in sees the same copy):
  mode        "browser" / "timeline" — which panel the dock shows. Both servers
              report it in /state; the pages tell the dock shell.
  lockOcc     the active occurrence's fullPathName ('' = root) while timeline mode
              is on, None otherwise. The list IS that component and its subtree, so
              leaving it is refused: Go Up / Go To Top / Go Down / Activate are
              vetoed at commandStarting, and a tick fallback re-activates it if
              something else moved it (never mid-command, never mid-sketch).
  push        push(cmd_id) -> bool: hands a Browser hotkey to THIS page. The Browser
              add-in's HotkeyStartingHandler calls it while mode == "timeline".

The walk runs only while timeline mode is on AND the page is polling. Its costly half
(enumerating the timeline, ~400 ms for 300 items) is redone only when (doc, count,
marker, active occurrence) changes or a non-view command finishes; once a second just
names, health and suppression are re-read off the cached objects (see _structure). Native timeline groups are flattened — the page's groups are its
own (groups.json).

PHASES (../Phases.py — read its header). Whatever the panel shows, this add-in also:
  • places every new timeline item at the end of its phase (_place_tick, every tick);
  • files every sketch into its component's "Sketches" group (_sketch_groups_tick);
  • serves the Browser's feature rows — a component's features, drawn in its Geometry
    group once that is opened — on /feats (_maybe_feats, only while the Browser polls it);
  • does the moves both pages ask for, never across a phase line (Phases.slot_ok):
    the panel's `probe`/`reorder`, the Browser's `featprobe`/`featreorder`, plus
    `setphase` (Phase ▸) and `sortphases` (Sort Timeline into Phases).
"""

import adsk.core, adsk.fusion
import builtins, importlib.util, json, os, subprocess, threading, time, traceback
import types as _types

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

EVENT_ID = 'FusionTimelineSyncEvent'
TOGGLE_CMD_ID = 'DockTimelineToggleCmd'
TRACE_CMD_ID = 'TimelineTracePickCmd'        # see "Trace a pick to its features"
SMART_DIMENSION_CMD_ID = 'SmartDimensionCmd'  # Space in a sketch (Smart Dimension add-in)
EDITOR_DIR = '/Users/josh/Code/Scripts/fusion_timeline'
SERVER_PY = os.path.join(EDITOR_DIR, 'server.py')

OCC_TYPE = 'adsk::fusion::Occurrence'
SKETCH_TYPE = 'adsk::fusion::Sketch'

# Commands that would take you out of the locked component. Vetoed while timeline mode
# is on (except mid-sketch, where Go Up is how you leave the sketch). The two "dual"
# go-in/go-out ids are handed to the page instead, where they open and shut groups.
LOCK_CMD_IDS = frozenset({
    'GoToParentComponentCmd', 'UnisolateTopLevelCmd', 'ActivateAndIsolateCmd',
    'FusionActivateLocalCompCmd',
    'thomasa88_anyShortcutListActivateContainingOrComponentCommand',
})
FORWARD_LOCKED = frozenset({'ActivateAndIsolateCmd', 'GoToParentComponentCmd'})

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


# The process-wide "someone is already in the model" latch — read its header.
tickguard = _load_local('tickguard', 'Tick Guard.py')
# The timeline's phase order and the moves that keep it — read its header.
phases = _load_local('phases', 'Phases.py')
# The one whole-item delete Browser, Timeline and Delete Selection share.
deleting = _load_local('deleting', 'Deleting.py')

# ── The shared dock state ─────────────────────────────────────────────────────
_DOCK = getattr(builtins, '_fusion_dock', None)
if not isinstance(_DOCK, dict):
    _DOCK = {}
    builtins._fusion_dock = _DOCK
_DOCK.setdefault('mode', 'browser')
_DOCK.setdefault('lockOcc', None)
_DOCK.setdefault('push', None)
_DOCK.setdefault('live', False)     # timeline mode AND its page polling (the lock's gate)

_server_mod = None
_server_mtime = 0.0
_httpd = None
_custom_evt = None
_active = False
_stop_evt = threading.Event()
_starting_handler = None
_terminated_handler = None


def _doc_key():
    """Stable per-document key: the dataFile id, or a name-based one when unsaved.
    Same as the Browser's, so both windows key a document the same way."""
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


# ── Server helpers (the Browser's, verbatim in shape) ─────────────────────────
def _load_server():
    global _server_mod, _server_mtime
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('fusion_timeline_server', SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _server_mod = mod
        try:
            _server_mtime = os.path.getmtime(SERVER_PY)
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
    """server.py lives outside AddIns, where Reload Recent AddIns cannot see it change —
    so the poller stat()s it and rebuilds the server when it moves. Pure Python, safe
    off the main thread (see the Browser's copy of this for the long version)."""
    global _server_mod
    if _server_mod is None:
        return
    try:
        stamp = os.path.getmtime(SERVER_PY)
    except OSError:
        return
    if stamp != _server_mtime:
        _stop_server()
        _server_mod = None
        _start_server()


def _push(cmd_id):
    """Hand a Browser hotkey to the page. False when the page isn't there to take it."""
    mod = _server_mod
    if mod is None or not mod.window_alive():
        return False
    mod.push_ui_action(cmd_id)
    return True


# ── Poller ────────────────────────────────────────────────────────────────────
# Fast while timeline mode is on (the page wants fresh rows) or something is queued;
# otherwise slow — the tick then only drains the queue (a toggle posted from the
# Browser page arrives here).
_FAST = 0.1
_SLOW = 0.3
_tick_pending = 0.0
_SERVER_CHECK_PERIOD = 2.0
_server_checked_at = 0.0


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
        mod = _server_mod
        busy = (_DOCK.get('mode') == 'timeline' or (mod is not None and (
            not mod._cmd_queue.empty() or mod.feats_alive())))
        _stop_evt.wait(_FAST if busy else _SLOW)


# ── Mode + lock ───────────────────────────────────────────────────────────────
def _active_path(design):
    try:
        occ = design.activeOccurrence
        return occ.fullPathName if occ else ''
    except Exception:
        return ''


def _set_mode(mode):
    """Swap the dock's panel. Turning the timeline on locks the active component."""
    if mode not in ('browser', 'timeline'):
        return
    design = adsk.fusion.Design.cast(app.activeProduct)
    if mode == 'timeline':
        # The timeline lives in the dock, so if `y` last swapped to Fusion's own
        # browser, swap back first (the Browser add-in hands us `show_dock`).
        show_dock = _DOCK.get('show_dock')
        if callable(show_dock):
            try:
                show_dock()
            except Exception:
                pass
        _DOCK['lockOcc'] = _active_path(design) if design else ''
        _DOCK['lockDoc'] = _doc_key()
    else:
        _DOCK['lockOcc'] = None
    # `live` is settled by the next tick, from whether the timeline page is polling.
    _DOCK['live'] = False
    _DOCK['mode'] = mode
    _sig['last'] = None                      # walk on the next tick
    if _server_mod:
        _server_mod.patch_state(locked=mode == 'timeline')


def _toggle_mode():
    _set_mode('browser' if _DOCK.get('mode') == 'timeline' else 'timeline')


def _editing_sketch(design):
    try:
        aeo = design.activeEditObject
        return bool(aeo) and aeo.objectType == SKETCH_TYPE
    except Exception:
        return False


def _sketch_open():
    """A sketch open at any depth, read live. SketchStop is the test hotkeys.py and
    Smart Dimension use (activeEditObject raises for a sketch nested in an occurrence);
    activeEditObject covers the beat after opening where SketchStop is still disabled."""
    try:
        cd = ui.commandDefinitions.itemById('SketchStop')
        if cd is not None and cd.controlDefinition.isEnabled:
            return True
    except Exception:
        pass
    design = adsk.fusion.Design.cast(app.activeProduct)
    return bool(design) and _editing_sketch(design)


def _occ_by_path(design, path):
    try:
        occs = design.rootComponent.allOccurrences
        for i in range(occs.count):
            o = occs.item(i)
            if o.fullPathName == path:
                return o
    except Exception:
        pass
    return None


_last_enforce = 0.0


def _enforce_lock(design):
    """The fallback: whatever moved the active component (a click in Fusion's own
    browser, another add-in), put it back. Never while a command runs or a sketch is
    open — editing a sub-component's sketch activates that component on purpose."""
    global _last_enforce
    lock = _DOCK.get('lockOcc')
    if lock is None or not _DOCK.get('live'):
        return
    try:
        if ui.activeCommand != 'SelectCommand' or _editing_sketch(design):
            return
    except Exception:
        return
    cur = _active_path(design)
    if cur == lock:
        return
    # Another document: the lock was a path in the one you left. Lock where this one is.
    doc = _doc_key()
    if doc != _DOCK.get('lockDoc'):
        _DOCK['lockOcc'], _DOCK['lockDoc'] = cur, doc
        return
    now = time.monotonic()
    if now - _last_enforce < 0.5:
        return
    _last_enforce = now
    with tickguard.hold('timeline/lock'):
        if lock == '':
            design.activateRootComponent()
            return
        occ = _occ_by_path(design, lock)
        if occ:
            occ.activate()
        else:
            _DOCK['lockOcc'] = cur          # the locked component is gone: stay here


# ── The walk ──────────────────────────────────────────────────────────────────
_sig = {'last': None, 'at': 0.0}
_REFRESH_S = 1.0


def _health_of(o):
    try:
        hs = o.healthState
        if hs == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
            return 'error'
        if hs == adsk.fusion.FeatureHealthStates.WarningFeatureHealthState:
            return 'warning'
    except Exception:
        pass
    return ''


def _timeline_objects(tl):
    """Every non-group item, in timeline order. Native groups are flattened (a group's
    children are read through it). Inside a COLLAPSED group `.index` raises
    (InternalValidationError: res >= 0) — the whole group holds one timeline slot — so
    such a child sorts as (group's slot, position in group). Where `.index` works it is
    the key, which also dedupes an item Fusion lists both at top level and in a group."""
    out = {}

    def add(o, pos, depth=0):
        try:
            try:
                pos = (o.index,)
            except Exception:
                pass
            if o.isGroup:
                g = adsk.fusion.TimelineGroup.cast(o)
                if g and depth < 20:
                    for j in range(g.count):
                        add(g.item(j), pos + (j,), depth + 1)
                return
            out[pos] = o
        except Exception:
            pass

    for i in range(tl.count):
        add(tl.item(i), (i,))
    return [out[k] for k in sorted(out)]


def _comps_in_view(design):
    """{compRef: {name, path, occTok}} for the active component and everything under it.
    `path` runs from just below the active component down; one occurrence per component
    (the first met), whose token is what an edit needs for assembly context."""
    act = design.activeComponent
    act_occ = design.activeOccurrence
    comps = {act.id: {'name': act.name, 'path': [], 'occTok': ''}}

    def rec(occs, path, depth):
        for i in range(occs.count):
            o = occs.item(i)
            try:
                c = o.component
                if c.id in comps:
                    continue
                p = path + [c.id]
                comps[c.id] = {'name': c.name, 'path': p, 'occTok': o.entityToken}
                if depth < 40:
                    rec(o.childOccurrences, p, depth + 1)
            except Exception:
                continue

    rec(act_occ.childOccurrences if act_occ else design.rootComponent.occurrences, [], 0)
    return comps


def _structure(design):
    """(comps, items) — the costly half of the walk. Timeline.item() is ~0.8 ms a call
    and TimelineObject.index ~0.45 ms (measured, M1 Pro), so a 300-item timeline costs
    ~400 ms of main thread: redone at 1 Hz it froze orbit/pan for 0.4 s of every second.
    Only redone when the structure may have moved (signature, a finished command, a
    forced re-walk); the 1 Hz refresh re-reads just the cheap per-row fields."""
    comps = _comps_in_view(design)
    act = design.activeComponent
    items = []
    for o in _timeline_objects(design.timeline):
        try:
            ent = o.entity
        except Exception:
            ent = None
        if ent is None:
            continue
        # Owner: features/sketches say parentComponent; construction planes/axes/points
        # only say .component. Nothing resolvable → skip; never guess the active one.
        try:
            otype = ent.objectType
            comp = ent.component if otype == OCC_TYPE else getattr(ent, 'parentComponent', None)
            if comp is None:
                comp = getattr(ent, 'component', None)
            ref = comp.id if comp else None
        except Exception:
            continue
        if ref is None or ref not in comps:
            continue
        # The active component's own occurrence (its creation / cut-paste) belongs to
        # the parent's timeline, not this one.
        if otype == OCC_TYPE and ref == act.id and design.activeOccurrence:
            continue
        try:
            tok = ent.entityToken
        except Exception:
            tok = ''
        items.append((o, ent, otype, ref, tok, phases.phase_of(o)))
    return comps, items


_struct = {'comps': None, 'items': None}

# An extrude's operation, for its icon (fusion_ui/tl_icons.js): 'cut' / 'intersect',
# '' for join / new body / new component (and anything that isn't an extrude). Read
# live each walk — changing the operation doesn't move the timeline signature.
_EXTRUDE = 'adsk::fusion::ExtrudeFeature'
_OPS = {adsk.fusion.FeatureOperations.CutFeatureOperation: 'cut',
        adsk.fusion.FeatureOperations.IntersectFeatureOperation: 'intersect'}


def _op_of(ent, otype):
    if otype != _EXTRUDE:
        return ''
    try:
        return _OPS.get(ent.operation, '')
    except Exception:
        return ''


def _walk(design, mod, rebuild):
    if rebuild or _struct['items'] is None:
        _struct['comps'], _struct['items'] = _structure(design)
    comps = _struct['comps']
    act = design.activeComponent
    rows, seen = [], {}
    for o, ent, otype, ref, tok, phase in _struct['items']:
        try:
            name = o.name
        except Exception:
            try:
                name = ent.name or ''
            except Exception:
                _struct['items'] = None      # a stale proxy: rebuild on the next tick
                continue
        name = name or '?'
        key = ref + '|' + name
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key += '#%d' % seen[key]
        health = _health_of(o)
        try:
            msg = o.errorOrWarningMessage if health else ''
        except Exception:
            msg = ''
        rows.append({
            'key': key, 'tok': tok, 'name': name, 'type': otype,
            'op': _op_of(ent, otype), 'icon': mod.icon_for(otype),
            'compRef': ref, 'compPath': comps[ref]['path'],
            'occTok': comps[ref]['occTok'],
            'health': health, 'msg': msg or '',
            'suppressed': bool(getattr(o, 'isSuppressed', False)),
            'past': bool(getattr(o, 'isRolledBack', False)),
            'phase': phase,
        })
    mod.update_state(rows, {
        'doc_id': _doc_key(), 'activeRef': act.id, 'activeName': act.name,
        'comps': comps, 'locked': _DOCK.get('lockOcc') is not None,
        'editing': _editing_sketch(design),
        'compIcon': mod.icon_for(OCC_TYPE),
        'phases': list(phases.PHASES),
    })


def _signature(design):
    try:
        count, marker = tickguard.timeline_pos(design)
        return (_doc_key(), count, marker, _active_path(design),
                _editing_sketch(design))
    except Exception:
        return None


def _maybe_walk(design, mod):
    sig = _signature(design)
    now = time.monotonic()
    rebuild = sig is None or sig != _sig['last'] or _struct['items'] is None
    if not rebuild and now - _sig['at'] < _REFRESH_S:
        return
    _sig['last'] = sig
    _sig['at'] = now
    t0 = time.perf_counter()
    _walk(design, mod, rebuild)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    mod.set_debug(**({'walk_ms': ms} if rebuild else {'refresh_ms': ms}))


# ── Commands from the page ────────────────────────────────────────────────────
def _find(design, tok):
    if not tok:
        return None
    try:
        found = design.findEntityByToken(tok)
        return found[0] if found and len(found) else None
    except Exception:
        return None


def _rename(design, tok, name):
    ent = _find(design, tok)
    if not ent or not name:
        return
    if ent.objectType == OCC_TYPE:
        ent.component.name = name
        return
    try:
        ent.name = name
    except Exception:
        ent.timelineObject.name = name


def _rename_comp(design, ref, name):
    comp = design.allComponents.itemById(ref) if ref else None
    if comp and name:
        comp.name = name


def _select(target, fallback):
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(target)
    except Exception:
        sel.add(fallback)


def _selected_sketch():
    """The sketch in the current selection (a proxy when picked in context), or None."""
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            sk = adsk.fusion.Sketch.cast(sel.item(i).entity)
            if sk:
                return sk
    except Exception:
        pass
    return None


# Fusion's generic Edit Feature (FusionEditFeatureCommand) does nothing when executed
# from the API — verified live 2026-09-24: with a feature picked it leaves SelectCommand
# running. Each feature type's own edit command does open it, so look that up by type.
# FusionExtrudeEditCommand is one the Extrude add-in's _EditStarting catches, so an
# extrude still opens its own panel. Anything unlisted falls back to the generic one.
_EDIT_CMD_BY_TYPE = {
    'ExtrudeFeature':           'FusionExtrudeEditCommand',
    'RevolveFeature':           'FusionRevolveEditCommand',
    'SweepFeature':             'FusionSweepEditCommand',
    'LoftFeature':              'FusionLoftEditCommand',
    'RibFeature':               'FusionDcRibEditCommand',
    'WebFeature':               'FusionDcWebEditCommand',
    'HoleFeature':              'FusionDcHoleEditCommand',
    'ThreadFeature':            'FusionDcThreadEditCommand',
    'FilletFeature':            'FusionDcFilletEditCommand',
    'ChamferFeature':           'FusionDcChamferEditCommand',
    'ShellFeature':             'FusionDcShellFeatureEditCommand',
    'DraftFeature':             'FusionDcDraftEditCommand',
    'ScaleFeature':             'FusionDcScaleEditCommand',
    'CombineFeature':           'FusionCombineEditCommand',
    'SplitBodyFeature':         'FusionDcSplitBodyEditCommand',
    'SplitFaceFeature':         'FusionDcSplitFaceEditCommand',
    'MoveFeature':              'FusionDcMoveCopyEditCommand',
    'OffsetFacesFeature':       'FusionOffsetFacesEditCommand',
    'ReplaceFaceFeature':       'FusionDcReplaceFaceEditCommand',
    'DeleteFaceFeature':        'FusionDcDeleteFaceEditCommand',
    'ThickenFeature':           'FusionDcSurfaceThickenEditCommand',
    'ExtendFeature':            'FusionDcSurfaceExtendEditCommand',
    'PatchFeature':             'FusionSurfacePatchEditCommand',
    'StitchFeature':            'FusionSurfaceStitchEditCommand',
    'UnstitchFeature':          'FusionSurfaceUnStitchEditCommand',
    'TrimFeature':              'FusionDcSurfaceTrimEditCommand',
    'UntrimFeature':            'FusionDcSurfaceUntrimEditCommand',
    'ReverseNormalFeature':     'FusionDcReverseNormalEdit',
    'EmbossFeature':            'FusionDcEmbossEditCmd',
    'RemoveFeature':            'FusionRemoveFeaturesEditCommand',
    'BoxFeature':               'BoxPrimitiveEditCommand',
    'CylinderFeature':          'CylinderPrimitiveEditCommand',
    'SphereFeature':            'SpherePrimitiveEditCommand',
    'TorusFeature':             'TorusPrimitiveEditCommand',
    'CoilFeature':              'CoilPrimitiveEditCommand',
    'PipeFeature':              'PipePrimitiveEditCommand',
    'MirrorFeature':            'FusionDcMirrorPatternEditCommand',
    'RectangularPatternFeature': 'FusionDcRectangularPatternEditCommand',
    'CircularPatternFeature':   'FusionDcCircularPatternEditCommand',
    'PathPatternFeature':       'FusionDcPathPatternEditCommand',
    'DerivedFeature':           'EditDeriveCommand',
}


def _edit_cmd_for(ent):
    kind = (getattr(ent, 'objectType', '') or '').split('::')[-1]
    return _EDIT_CMD_BY_TYPE.get(kind, 'FusionEditFeatureCommand')


# Features the API can't select (a sheet-metal Flange: "invalid argument entity") and
# with no edit command of their own: select one of the faces they made and run the
# face's Edit Feature (FusionEditDcFeatureCommand) — verified live 2026-09-25, it opens
# FusionBaseFaceEditCommand on the flange. The generic one does nothing from a face.
_EDIT_BY_FACE = {'adsk::fusion::FlangeFeature': 'FusionEditDcFeatureCommand'}


def _face_occ(ent, occ):
    """*occ*, or — for a feature outside the root — the first occurrence of its
    component: its faces can't be selected in the component's own context."""
    comp = ent.parentComponent
    design = comp.parentDesign
    if occ is None and comp.id != design.rootComponent.id:
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        occ = occs.item(0) if occs.count else None
    return occ


def _select_a_face(ent, occ):
    fc = ent.faces.item(0)
    occ = _face_occ(ent, occ)
    if occ is not None:
        fc = fc.createForAssemblyContext(occ)
    sel = ui.activeSelections
    sel.clear()
    sel.add(fc)


def _select_faces_of(ent, occ):
    """Add every face *ent* made to the selection (a feature the API can't select)."""
    occ = _face_occ(ent, occ)
    sel = ui.activeSelections
    for fc in ent.faces:
        try:
            sel.add(fc.createForAssemblyContext(occ) if occ is not None else fc)
        except Exception:
            pass


def _edit(design, tok, occ_tok, sketch):
    """Edit Sketch: the Browser's enterSketch pattern (activate the owner if it isn't
    the active component, select, SketchActivate). Edit Feature: select the feature and
    run that feature type's own edit command (_EDIT_CMD_BY_TYPE)."""
    ent = _find(design, tok)
    if not ent:
        _hud('fail', "That feature isn't in the model any more.")
        return
    occ = _find(design, occ_tok) if occ_tok else None
    target = ent
    if occ is not None and hasattr(ent, 'createForAssemblyContext'):
        try:
            target = ent.createForAssemblyContext(occ)
        except Exception:
            target = ent
    if sketch:
        _open_sketch(design, ent, target, occ)
        return
    try:
        if ent.objectType in _EDIT_BY_FACE:
            _select_a_face(ent, occ)
            cd = ui.commandDefinitions.itemById(_EDIT_BY_FACE[ent.objectType])
        else:
            _select(target, ent)
            cd = ui.commandDefinitions.itemById(_edit_cmd_for(ent))
        if not cd:
            raise RuntimeError('no edit command')
        cd.execute()
    except Exception:
        if _server_mod:
            _server_mod.set_debug(last_error=traceback.format_exc())
        name = getattr(ent, 'name', '') or 'That feature'
        _hud('fail', "%s couldn't be opened for editing." % name)


def _edit_traced(design, tok):
    """The Browser's Edit on a Trace selection (its first press ran _trace_pick, as Go
    In's does): edit that feature, in the occurrence the trace found it in. A selection
    Trace didn't make (a timeline row's click) edits *tok* where it stands."""
    t = _trace
    if t['cands'] and _same_sel(t['sel'], _sel_entities()):
        ttok, occ_tok = t['cands'][t['i']]
        if not tok or ttok == tok:
            _edit(design, ttok, occ_tok, False)
            return
    if tok:
        _edit(design, tok, '', False)


def _open_sketch(design, sk, target, occ):
    try:
        if occ is not None and sk.parentComponent.id != design.activeComponent.id:
            occ.activate()
    except Exception:
        pass
    _select(target, sk)
    cd = ui.commandDefinitions.itemById('SketchActivate')
    if cd:
        cd.execute()


def _sketch_in(obj, depth=0):
    """The first sketch *obj* leads to: its own parentSketch (Profile, SketchCurve,
    SketchPoint, SketchText), else the first of its items (a collection, a Path) or of
    what they wrap (PathEntity.entity, LoftSection.entity). A face or an edge leads
    nowhere."""
    if obj is None or depth > 3:
        return None
    try:
        sk = getattr(obj, 'parentSketch', None)
        if sk is not None:
            return sk
    except Exception:
        pass
    if hasattr(obj, 'count') and hasattr(obj, 'item'):
        try:
            for i in range(obj.count):
                sk = _sketch_in(obj.item(i), depth + 1)
                if sk is not None:
                    return sk
        except Exception:
            pass
        return None
    try:
        return _sketch_in(getattr(obj, 'entity', None), depth + 1)
    except Exception:
        return None


# Where a feature keeps what it was built from, most telling first. Extrude/Revolve/
# Coil/Rib/Web: profile; Emboss: inputProfiles; Sweep/Pipe: path (and guideRail); Loft:
# loftSections. Mirror, Pattern, Fillet… take faces, bodies or edges — no sketch.
_SOURCE_ATTRS = ('profile', 'profiles', 'inputProfiles', 'path', 'loftSections',
                 'guideRail', 'centerLineOrRails')

# Sketch-made features the API gives no input for: a sheet-metal Flange (contour or
# face flange) has only faces and bodies. Its sketch is found by shape (_sketch_on_body).
_OPAQUE_SKETCH_TYPES = frozenset(('adsk::fusion::FlangeFeature',))


def _source_sketch(ent):
    for a in _SOURCE_ATTRS:
        try:
            if hasattr(ent, a):
                sk = _sketch_in(getattr(ent, a))
                if sk is not None:
                    return sk
        except Exception:                        # rolled back, or an input that's gone
            pass
    try:
        if ent.objectType in _OPAQUE_SKETCH_TYPES:
            return _sketch_on_body(ent)
    except Exception:
        pass
    return None


def _sketch_on_body(feat):
    """The latest sketch of the feature's component, before it in the timeline, whose
    real curves (not construction, not projected) ALL lie on the feature's body — a
    contour flange's path runs along its edge, a face flange's profile on its face.
    Three points a curve (ends and middle); a sketch only partly on it is someone
    else's (measured 2026-09-25: the true contour sketch 6/6, the one before it 7/21)."""
    bodies = _coll(feat.bodies)
    if not bodies:
        return None
    own = _tl_index(feat.timelineObject)
    on = adsk.fusion.PointContainment.PointOnPointContainment
    best = None
    for sk in _coll(feat.parentComponent.sketches):
        try:
            idx = _tl_index(sk.timelineObject)
            if idx >= own or (best is not None and idx <= best[0]):
                continue
            pts = []
            for c in _coll(sk.sketchCurves):
                if c.isConstruction or c.isReference:
                    continue
                ev = c.worldGeometry.evaluator
                _ok, a, b = ev.getParameterExtents()
                for t in (0.0, 0.5, 1.0):
                    ok, p = ev.getPointAtParameter(a + (b - a) * t)
                    if ok:
                        pts.append(p)
            if pts and all(any(bd.pointContainment(p) == on for bd in bodies) for p in pts):
                best = (idx, sk)
        except Exception:
            pass
    return best[1] if best else None


def _edit_source(design, tok, occ_tok):
    """F on a Browser feature row: edit the sketch the feature was made from. The
    sketch's occurrence is the feature's when they share a component, else the first
    occurrence of the sketch's own (the root's sketches need the root active). No
    sketch behind it → the HUD cross, like Sketch Clipboard's refusals — a fillet,
    chamfer or shell always, whatever sketch is selected: the sketch of a feature it
    touches is not its own (see _select_feature).
    A copy (pattern, move) edits its original's sketch (_input_sketch). A MIRROR edits
    only a sketch it names itself — never one found by following what it copied.
    When the feature's sketch is the one SELECTED (the row's click selected it), that
    selection is what opens — it already carries the right occurrence."""
    ent = _find(design, tok)
    if not ent:
        return
    mirror = ent.objectType == 'adsk::fusion::MirrorFeature'
    sk = _source_sketch(ent) or (None if mirror else _input_sketch(design, ent))
    if sk is None:
        name = getattr(ent, 'name', '') or 'This feature'
        _hud('fail', "%s wasn't made from a sketch." % name)
        return
    picked = _selected_sketch()
    if picked is not None:
        try:
            psk = picked.nativeObject or picked
            occ = picked.assemblyContext
            if psk == sk:
                if occ is None and design.activeComponent.id != sk.parentComponent.id \
                        and sk.parentComponent.id == design.rootComponent.id:
                    design.activateRootComponent()
                _open_sketch(design, sk, picked, occ)
                return
        except Exception:
            pass
    try:
        comp = sk.parentComponent
        if comp.id == design.rootComponent.id and design.activeComponent.id != comp.id:
            design.activateRootComponent()
    except Exception:
        pass
    target, occ = _sketch_target(design, ent, sk, occ_tok)
    _open_sketch(design, sk, target, occ)


def _sketch_target(design, ent, sk, occ_tok):
    """(sketch proxy, its occurrence) for feature *ent*'s source sketch *sk*: the
    feature's occurrence when they share a component, else the first occurrence of the
    sketch's own; the root's sketches are native (occurrence None)."""
    comp = sk.parentComponent
    occ = None
    try:
        if comp.id == ent.parentComponent.id and occ_tok:
            occ = _find(design, occ_tok)
        elif comp.id != design.rootComponent.id:
            occs = design.rootComponent.allOccurrencesByComponent(comp)
            occ = occs.item(0) if occs.count else None
    except Exception:
        pass
    target = sk
    if occ is not None:
        try:
            target = sk.createForAssemblyContext(occ)
        except Exception:
            target = sk
    return target, occ


HUD_FLASH = '/Users/josh/bin/hud_flash'


def _hud(kind, message=None):
    """Sketch Clipboard's HUD: a soft tick/cross that fades on its own; a 'fail' with
    a message opens it in a dialog if clicked. Fire and forget."""
    try:
        args = [HUD_FLASH, kind] + ([message] if message else [])
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


def _roll_to(design, mod, tok):
    """The Browser's Roll Timeline Here: the marker to just AFTER the feature, so it and
    everything before it compute and the rest rolls back (the Browser dots it)."""
    why = _move_blocked(design)
    if why:
        return _notice(mod, why)
    ent = _find(design, tok) if tok else None
    if ent is None:
        return _notice(mod, "Couldn't find that feature.")
    try:
        ent.timelineObject.rollTo(False)
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())
        _notice(mod, "Couldn't roll the timeline there.")


def _suppress(design, toks, on):
    """The API setter first. In a design that already has compute errors anywhere it
    throws (RuntimeError 3 listing every failing feature) and the change is refused, so
    whatever didn't flip goes to Fusion's own timeline Suppress/Unsuppress command,
    selected — the same one the native timeline's right-click runs."""
    on = bool(on)
    left = []
    for tok in toks or []:
        ent = _find(design, tok)
        if ent is None:
            continue
        try:
            ent.timelineObject.isSuppressed = on
        except Exception:
            if _server_mod:
                _server_mod.set_debug(last_error=traceback.format_exc())
        try:
            if bool(ent.timelineObject.isSuppressed) != on:
                left.append(ent)
        except Exception:
            left.append(ent)
    if not left:
        return
    sel = ui.activeSelections
    sel.clear()
    for e in left:
        try:
            sel.add(e)
        except Exception:
            pass
    cd = ui.commandDefinitions.itemById(
        'FusionSuppressCommand' if on else 'FusionUnsuppressCommand')
    try:
        if sel.count and cd:
            cd.execute()
    except Exception:
        if _server_mod:
            _server_mod.set_debug(last_error=traceback.format_exc())


def _delete(design, toks):
    """Delete the rows the page asked for (⌘D / the menu) — through the one delete the
    Browser and Delete Selection share too (Deleting.py; read its header)."""
    ents = [e for e in (_find(design, tok) for tok in toks or []) if e is not None]
    if not ents:
        return
    try:
        deleting.delete(ui, design, ents)
    except Exception:
        if _server_mod:
            _server_mod.set_debug(last_error=traceback.format_exc())


# ── Reordering (drag) ─────────────────────────────────────────────────────────
# The page never decides what may move where: Fusion does, through the same
# TimelineObject.canReorder / reorder the native bar's drag goes through, so its
# dependency rules are the rules here. `probe` answers, when a drag STARTS, which
# drop spots Fusion would take (the page shows no mark on the rest); `reorder` asks
# again for every object at drop time and only then moves anything.

def _move_blocked(design):
    """Why nothing can be reordered right now, or ''. The native bar is dead while a
    command runs or a sketch is open for edit; so is this."""
    try:
        if _editing_sketch(design):
            return 'Finish the sketch first.'
        if ui.activeCommand != 'SelectCommand':
            return 'Finish the command first.'
    except Exception:
        return 'Fusion is busy.'
    return ''


def _tl_obj(design, tok):
    """(timeline object, its index) for a row's token; None when it has no index of its
    own — gone, or inside a COLLAPSED native group, where .index raises and the native
    bar would only move the whole group."""
    ent = _find(design, tok)
    if ent is None:
        return None
    try:
        o = ent.timelineObject
        return (o, o.index) if o else None
    except Exception:
        return None


def _movers(design, toks):
    """The dragged objects in timeline order, or None if any can't be moved alone."""
    got = []
    for tok in toks or []:
        t = _tl_obj(design, tok)
        if t is None:
            return None
        got.append(t)
    got.sort(key=lambda t: t[1])
    return [o for o, _ in got]


def _can_all(objs, idx):
    for o in objs:
        try:
            if not o.canReorder(idx):
                return False
        except Exception:
            return False
    return True


def _later(tl, objs, idx):
    """Is before-index `idx` (-1 = end) after every one of `objs` — a LATER move?"""
    at = tl.count if idx is None or idx < 0 else idx
    return all(o.index < at for o in objs)


def _can_go(tl, objs, idx):
    """A drop spot worth offering: Fusion takes it as-is, or it's a later move, where
    what depends on the movers can be taken along (_carry)."""
    return _later(tl, objs, idx) or _can_all(objs, idx)


def _carry(design, objs, idx, notice):
    """A later move Fusion refused as-is: make it anyway, carrying along whatever after
    the movers depends on them (Phases.carry_later). Last mover first, each landing
    just before the one after it, so they keep their order and arrive together."""
    tl = design.timeline
    dest = None if idx is None or idx < 0 else tl.item(idx)
    try:
        for o in reversed(objs):
            phases.carry_later(tl, o, dest)
            dest = o
    except phases.Refused as r:
        notice("Fusion won't move %s there, even taking along what depends on it." % r.name)


def _probe(design, mod, pid, toks, targets):
    t0 = time.perf_counter()
    ok, end, why = [], False, _move_blocked(design)
    objs = None if why else _movers(design, toks)
    if not why and objs is None:
        why = "That's inside a collapsed Fusion group — expand it in Fusion first."
    if objs:
        for tok in targets or []:
            t = _tl_obj(design, tok)
            if (t is not None and phases.slot_ok(design.timeline, objs, t[1])
                    and _can_go(design.timeline, objs, t[1])):
                ok.append(tok)
        end = phases.slot_ok(design.timeline, objs, -1) and _can_go(design.timeline, objs, -1)
    mod.patch_state(probe={'id': pid, 'ok': ok, 'end': end, 'why': why,
                           'ms': round((time.perf_counter() - t0) * 1000, 1)})


def _reorder(design, mod, toks, before):
    """Move `toks` (kept in their timeline order) to just before the row `before`, or to
    the end of the timeline when it is ''. Each one goes before the target in turn, so
    they arrive together in the order they had."""
    def notice(text):
        mod.patch_state(notice={'at': time.time(), 'text': text})
    why = _move_blocked(design)
    objs = None if why else _movers(design, toks)
    if why or objs is None:
        return notice(why or "Can't move that — it's inside a collapsed Fusion group.")
    if before:
        t = _tl_obj(design, before)
        if t is None:
            return notice("Can't drop there.")
        idx = t[1]
    else:
        idx = -1
    if not phases.slot_ok(design.timeline, objs, idx):
        return notice(_PHASE_LINE)
    if not _can_all(objs, idx):
        if _later(design.timeline, objs, idx):
            return _carry(design, objs, idx, notice)
        return notice("Fusion won't allow that order (a dependency would break).")
    for n, o in enumerate(objs):
        if before:
            t = _tl_obj(design, before)
            idx = t[1] if t else -1
        try:
            moved = o.canReorder(idx) and o.reorder(idx)
        except Exception:
            moved = False
        if not moved:
            return notice('Moved %d of %d — Fusion refused the rest.' % (n, len(objs)))


_PHASE_LINE = "Can't move across a phase line (see Phase ▸ on the row's menu)."


# ── Placement: new items go to the end of their phase ─────────────────────────
# Runs on every tick, panel shown or not. Fusion inserts at the marker, so while the
# marker sits at the end a grown count means the new items are the tail. That is
# checked, not assumed: the item that WAS last must still be at the same spot (Masters
# inserts a sketch earlier and puts the marker back — its tail moved, so hands off).
# Nothing happens mid-command or mid-sketch; the baseline waits for it to finish, so a
# command's preview items never count, and a sketch is placed when you leave it.
# A document seen for the first time is only baselined — no history is touched.
_place = {'doc': None, 'count': 0, 'atEnd': False, 'tail': ''}
_last_cmd = {'id': '', 'at': 0.0}


def _tail_sig(tl, n):
    if n <= 0:
        return ''
    try:
        o = tl.item(n - 1)
        ent = None if o.isGroup else o.entity
        return '%s|%s' % (o.name, ent.entityToken if ent is not None else '')
    except Exception:
        return None


def _notice(mod, text):
    n = {'at': time.time(), 'text': text}
    mod.patch_state(notice=n)
    mod.patch_feats(notice=n)


def _place_tick(design, mod):
    try:
        tl = design.timeline
        doc = _doc_key()
        n, m = tickguard.timeline_pos(design)   # shared; re-read directly after placing
    except Exception:
        return
    st = _place
    if doc != st['doc']:
        st.update(doc=doc, count=n, atEnd=m == n, tail=_tail_sig(tl, n))
        return
    if n == st['count'] and (m == n) == st['atEnd']:
        return
    if _move_blocked(design):
        return                               # compare once it has finished
    grew = (n > st['count'] and st['atEnd'] and m == n
            and _tail_sig(tl, st['count']) == st['tail'])
    if grew:
        hint = _last_cmd['id'] if time.monotonic() - _last_cmd['at'] < 5.0 else ''
        with tickguard.hold('timeline/place'):
            notes = phases.place_new(design, st['count'], hint)
        if notes:
            _notice(mod, ' '.join(notes))
        _sig['last'] = None
        _feats_state['dirty'] = True
        n, m = tl.count, tl.markerPosition
    st.update(count=n, atEnd=m == n, tail=_tail_sig(tl, n))


# ── Sketches groups: every sketch filed in its component's "Sketches" ─────────
# The rule and the one-time tidy of hand-made groups are the server's (file_sketches,
# pure over groups.json). This only reads each component's sketch names — ~2 ms for
# 130 sketches on the big doc — and only when something could have changed: the
# timeline (a new or rolled-forward sketch), or the groups (a page wrote a copy that
# may have lost a filing, or dissolved Sketches). Never mid-command or mid-sketch, so a
# sketch is filed once it is finished — and once it has its real name.
_sk_sig = {'last': None}
_BOUNDS_SKETCHES = frozenset('Bounds ' + a for a in ('X', 'Y', 'Z', 'XY', 'XZ', 'YZ'))


def _sketch_keys(design):
    """{compRef: {rowKey: is_bounds | "plane"}} — keys as _walk makes them (compRef|name, #n for
    a repeated name), for every component with sketches."""
    out = {}
    for c in design.allComponents:
        sks = c.sketches
        n = sks.count
        planes = c.constructionPlanes
        np_ = planes.count
        if not n and not np_:
            continue
        ref, seen, keys = c.id, {}, {}
        for i in range(n):
            name = sks.item(i).name or '?'
            seen[name] = seen.get(name, 0) + 1
            key = ref + '|' + name + ('#%d' % seen[name] if seen[name] > 1 else '')
            keys[key] = name in _BOUNDS_SKETCHES
        # A component's construction planes are filed too — value "plane" (the server
        # puts them in a "Planes" group beside "Sketches"). Keyed as _walk keys every
        # timeline item, sharing the one name counter with the sketches.
        for i in range(np_):
            name = planes.item(i).name or '?'
            seen[name] = seen.get(name, 0) + 1
            key = ref + '|' + name + ('#%d' % seen[name] if seen[name] > 1 else '')
            keys[key] = 'plane'
        out[ref] = keys
    return out


def _sketch_groups_tick(design, mod):
    try:
        # The 5 s bucket catches what neither moves: a sketch renamed in Fusion's own
        # browser while no page was open to carry its filing.
        sig = (_doc_key(), tickguard.timeline_pos(design), mod._groups_rev,
               int(time.monotonic() // 5))
    except Exception:
        return
    if sig == _sk_sig['last'] or not sig[0] or _move_blocked(design):
        return
    _sk_sig['last'] = sig
    try:
        mod.file_sketches(sig[0], _sketch_keys(design))
    except Exception:
        mod.set_debug(sketch_groups_error=traceback.format_exc())


# ── Feature rows for the Browser ──────────────────────────────────────────────
# The Browser draws a component's timeline features in its Geometry group once you open
# it. It posts `watch` with the open groups ([{key, comp}]); while it polls /feats they
# are rebuilt here whenever the timeline moves, a command finishes, or once every 2 s
# (for health / suppression). Only the watched components are scanned, and each group
# holds that component's own features — the ones made IN it — less any whose bodies
# are all in other components (_bodies_elsewhere), plus any made elsewhere that were
# tagged as reaching into it (_also_in: a cross-component Combine).
_feats_state = {'watch': [], 'sig': None, 'at': 0.0, 'dirty': True}
_FEATS_REFRESH_S = 2.0


def _same_body(a, b):
    """The same B-Rep entity (body, face, edge): == on the native objects, so a proxy
    picked in an occurrence still matches its native. Never by entityToken: this is
    called for every face and edge of a trace or a row click, almost always on two
    DIFFERENT entities, and a token read is slow (and leaks) — with the token fallback
    a row click on a rib-grid cut spent seconds, a whole-body walk minutes, in it."""
    try:
        if a == b:
            return True
        na = getattr(a, 'nativeObject', None) or a
        nb = getattr(b, 'nativeObject', None) or b
        return (na is not a or nb is not b) and na == nb
    except Exception:
        return False


def _component_features(design, comp):
    """Every feature of `comp`: its Features collection, or — if that can't be listed —
    the timeline's features whose owner is `comp`."""
    out = []
    try:
        fs = comp.features
        for i in range(fs.count):
            out.append(fs.item(i))
        return out
    except Exception:
        out = []
    for o in _timeline_objects(design.timeline):
        try:
            ent = o.entity
            if (ent is not None and ent.objectType.endswith('Feature')
                    and ent.parentComponent.id == comp.id):
                out.append(ent)
        except Exception:
            pass
    return out


def _dup_suffixes(comp):
    """{entityToken: '#n'} for the items of `comp` whose name an earlier one of it
    shares — the suffix _walk gives the second and later of them in timeline order, so
    a feature row here carries the same key as its timeline row (and so its groups)."""
    ents = []
    for attr in ('features', 'sketches', 'constructionPlanes', 'constructionAxes',
                 'constructionPoints'):
        try:
            coll = getattr(comp, attr)
            for i in range(coll.count):
                e = coll.item(i)
                ents.append((e.name, e))
        except Exception:
            pass
    count = {}
    for n, _ in ents:
        count[n] = count.get(n, 0) + 1
    out = {}
    for name in [n for n, k in count.items() if k > 1]:
        same = []
        for n, e in ents:
            if n != name:
                continue
            try:
                same.append((e.timelineObject.index, e.entityToken))
            except Exception:
                pass
        for j, (_, tok) in enumerate(sorted(same)):
            if j:
                out[tok] = '#%d' % (j + 1)
    return out


def _feat_rows(design, watch, mod):
    """{watch key: [row, …]} — every feature of each watched component (the Browser's
    open Geometry groups, [{key, comp: Component.id}]), in timeline order."""
    by_id = {}
    try:
        for c in design.allComponents:
            by_id[c.id] = c
    except Exception:
        return {}
    comps = {}
    for w in watch or []:
        c = by_id.get(str(w.get('comp') or ''))
        if c is not None:
            comps[str(w.get('key') or '')] = c
    if not comps:
        return {}
    out = {k: [] for k in comps}
    dups = {}
    also = _also_in(design)
    for k, c in comps.items():
        for f in _component_features(design, c):
            if _bodies_elsewhere(f, c.id):
                continue
            row = _feat_row(f, dups, mod)
            if row is not None:
                out[k].append(row)
        for f in also.get(c.id, ()):
            row = _feat_row(f, dups, mod)
            if row is not None:
                out[k].append(row)
    return {k: [r for _, r in sorted(v, key=lambda t: t[0])] for k, v in out.items()}


# A cross-component Combine (AddIns/Combine) is made in its target's component and
# tagged with the other components it took tools from — spelled as Combine.py does.
ALSO_IN_GROUP, ALSO_IN_NAME = 'ScriptsCombine', 'alsoIn'


def _also_in(design):
    """{component id: [feature, …]} — the features made in ANOTHER component that are
    listed under this one too (the tag above). One findAttributes for the design."""
    out = {}
    try:
        attrs = design.findAttributes(ALSO_IN_GROUP, ALSO_IN_NAME)
    except Exception:
        return out
    for a in attrs:
        try:
            f = a.parent
            if f is None or not f.isValid:
                continue
            for cid in json.loads(a.value or '[]'):
                out.setdefault(str(cid), []).append(f)
        except Exception:
            pass
    return out


def _bodies_elsewhere(f, comp_id):
    """True when every body the feature reports lives in another component — it is not
    a feature of this component's bodies. A feature with none (rolled back, its body
    since consumed, a cut) stays: nothing says it belongs anywhere else. A Combine lists
    its tool bodies too, so ONE body here is enough to keep it."""
    try:
        bs = f.bodies
        n = bs.count
    except Exception:
        return False
    if not n:
        return False
    for i in range(n):
        try:
            b = bs.item(i)
            b = b.nativeObject or b
            if b.parentComponent.id == comp_id:
                return False
        except Exception:
            return False
    return True


def _bodies_unseen(f):
    """True when every body the feature reports is not drawn (isVisible — its own bulb,
    the Bodies folder, a hidden occurrence above it): it only touches geometry you can't
    see, and the Browser strikes it. None reported (rolled back, suppressed) says
    nothing, so False; a consumed body (a Combine's tool) isn't counted."""
    try:
        bs = f.bodies
        n = bs.count
    except Exception:
        return False
    seen = 0
    for i in range(n):
        try:
            b = bs.item(i)
            if not b.isValid:
                continue
            if b.isVisible:
                return False
            seen += 1
        except Exception:
            return False
    return seen > 0


def _feat_row(f, dups, mod):
    """(timeline index, the Browser's row) for one feature, or None."""
    try:
        o = f.timelineObject
        health = _health_of(o)
        comp = f.parentComponent
        if comp.id not in dups:
            dups[comp.id] = _dup_suffixes(comp)
        name = o.name or f.name or '?'
        tok = f.entityToken
        return o.index, {
            'tok': tok, 'name': name, 'type': f.objectType,
            'op': _op_of(f, f.objectType),
            'key': comp.id + '|' + name + dups[comp.id].get(tok, ''),
            'compRef': comp.id, 'icon': mod.icon_for(f.objectType),
            'phase': phases.phase_of(o),
            'pinned': phases.override_of(f) is not None,
            'health': health,
            'msg': (o.errorOrWarningMessage or '') if health else '',
            'suppressed': bool(o.isSuppressed), 'past': bool(o.isRolledBack),
            'unseen': _bodies_unseen(f),
        }
    except Exception:
        return None


def _maybe_feats(design, mod):
    fs = _feats_state
    if not fs['watch'] or not mod.feats_alive():
        return
    sig = _signature(design)
    now = time.monotonic()
    if not (fs['dirty'] or sig != fs['sig'] or now - fs['at'] > _FEATS_REFRESH_S):
        return
    fs.update(dirty=False, sig=sig, at=now)
    t0 = time.perf_counter()
    mod.patch_feats(feats=_feat_rows(design, fs['watch'], mod), phases=list(phases.PHASES),
                    doc_id=_doc_key(), reorder=fs.get('reorder'))
    mod.set_debug(feats_ms=round((time.perf_counter() - t0) * 1000, 1))


def _select_feature(design, tok, occ_tok):
    """Select a feature the way a click on the native timeline does, plus the sketch
    it was made from — selected, not shown: a hidden sketch still draws its selection
    highlight, which is the point (where the feature came from, without the sketch).
    A copy (pattern, mirror, move) gets its original's sketch (_input_sketch). A
    feature with no sketch of its own (fillet, chamfer, shell…) gets none: the sketch
    of a feature it touches — the base extrude under a chamfer — belongs to the faces
    round it, and you'd click one of those if that sketch were what you wanted."""
    ent = _find(design, tok)
    if not ent:
        return
    if ent.objectType == 'adsk::fusion::MirrorFeature':
        # Selecting a mirror lights up the whole body. Its row is still picked (the
        # page holds it), and delete / suppress work off the row, not the viewport.
        ui.activeSelections.clear()
        return
    occ = _find(design, occ_tok) if occ_tok else None
    target = ent
    if occ is not None and hasattr(ent, 'createForAssemblyContext'):
        try:
            target = ent.createForAssemblyContext(occ)
        except Exception:
            target = ent
    try:
        _select(target, ent)
    except Exception:
        # A sheet-metal Flange can't be selected from the API at all ("invalid
        # argument entity", native or timeline object — 2026-09-25): its faces stand
        # in for it, the highlight a selected feature would have given, then its sketch.
        _select_faces_of(ent, occ)
    sk = _source_sketch(ent) or _input_sketch(design, ent)
    if sk is None:
        return
    sk_target, _occ = _sketch_target(design, ent, sk, occ_tok)
    try:
        ui.activeSelections.add(sk_target)
    except Exception:
        try:
            ui.activeSelections.add(sk)
        except Exception:
            pass


# ── Trace a pick to its features ──────────────────────────────────────────────
# TRACE_CMD_ID with a face, edge, vertex or body picked: select the latest timeline
# feature behind it, the way a feature row's click does (_select_feature — the feature
# plus the sketch it was made from), plus the edges that feature made and any later
# Mirror's copies of them (_made_edges, _mirrored_edges — none past _MAX_EDGES). Run again while that selection still stands and it
# edits that feature (_edit) — the first pick is taken as right; it never steps back.
#   edge   → the feature that MADE it: of the features owning its two faces (.faces),
#            the latest — an edge exists from the moment its second face does, so a
#            hole's rim is the cut's, never the plate's it was cut into
#   vertex → the makers of its edges
#   face   → the features owning it — what generated the face, never what was cut
#            through it (a plate's top face is the plate's, not the hole's)
#   body   → every feature that touches it (Phases._bodies_of)
#   sketch → (the sketch, or a curve / point / profile of it) the EARLIEST feature made
#            from it (_sketch_feature) — then the same selection as a body pick on it
# A face, edge or vertex nothing claims falls back to its body's features.
# Never matched: Mirror features — a part a mirror made is traced on its other side
# instead (_mirror_twin), so both halves land on the same feature and sketch.
# The body's first feature is left out of a body-wide list (body pick, or a part
# nothing claims) unless it's the only one; a part it made directly keeps it.
# Rolled-back and suppressed items are skipped: their
# faces aren't in the model.
# A feature with no sketch of its own (fillet, chamfer, shell…) is selected without
# one — the same for a Browser feature-row click (_select_feature).
# The Browser is handed the feature (dock['quiet_select']): it picks that feature's
# rows (under bodies opened with F), not the sketch's, and neither opens the tree to
# them nor jumps to the component.
_trace = {'cands': [], 'i': 0, 'sel': None}


def _sel_entities():
    sel = ui.activeSelections
    out = []
    for i in range(sel.count):
        try:
            out.append(sel.item(i).entity)
        except Exception:
            pass
    return out


def _selected_feature(sel):
    """(token, occurrence token) of the first timeline feature in *sel*, else None.
    Sketches don't count: a trace selects one alongside its feature."""
    for e in sel:
        try:
            if e.objectType == 'adsk::fusion::Sketch' or not hasattr(e, 'timelineObject'):
                continue
            occ = getattr(e, 'assemblyContext', None)
            nat = getattr(e, 'nativeObject', None) or e
            return nat.entityToken, occ.entityToken if occ is not None else ''
        except Exception:
            continue
    return None


def _same_sel(a, b):
    return a is not None and len(a) == len(b) and all(_same_body(x, y) for x, y in zip(a, b))


def _pick(ent):
    """(native entity, native body, occurrence) for a picked face / edge / vertex /
    body; body None when the pick is none of those."""
    occ = getattr(ent, 'assemblyContext', None)
    native = getattr(ent, 'nativeObject', None) or ent
    if isinstance(native, adsk.fusion.BRepBody):
        return native, native, occ
    if isinstance(native, (adsk.fusion.BRepFace, adsk.fusion.BRepEdge,
                           adsk.fusion.BRepVertex)):
        return native, native.body, occ
    return None, None, None


def _tl_index(o):
    try:
        return o.index
    except Exception:                 # inside a collapsed group: the group's slot
        return o.parentGroup.index


def _live_features(design, comp):
    """(timeline index, feature) for every feature of comp that's in the model now."""
    out = []
    for f in _component_features(design, comp):
        try:
            o = f.timelineObject
            if not (o.isRolledBack or o.isSuppressed):
                out.append((_tl_index(o), f))
        except Exception:
            pass
    return out


def _coll(c):
    return [c.item(i) for i in range(c.count)]


# Feature.faces is SLOW for a big feature: each .item(i) rebuilds the list, ~0.03 ms
# per face in it — a 1,300-face pattern costs ~40 ms a face, ~40 s to read whole
# (measured 2026-09-24; a body's own .faces is ~0.1 ms a face flat). So a feature with
# more than _BIG faces is never read whole: only its first _BIG_SAMPLE faces, as shapes.
_BIG = 64
_BIG_SAMPLE = 6


def _feat_faces(f):
    """(faces read, whole?) — all of a small feature's faces, a sample of a big one's."""
    fc = f.faces
    n = fc.count
    if n > _BIG:
        return [fc.item(i) for i in range(_BIG_SAMPLE)], False
    return [fc.item(i) for i in range(n)], True


class _FaceIndex:
    """Which of one component's live features own which faces (Feature.faces), looked
    up by tempId (one call per face, once) and confirmed with ==. Big features (see
    _BIG) are known only by a sample of face shapes; a face no small feature claims is
    put down to the big features on its body — the one whose sample holds its shape,
    else the latest."""
    def __init__(self, design, comp):
        self.feats = _live_features(design, comp)
        self.by_tid = {}
        self.faces = {}                    # id(feature) -> faces read (see _feat_faces)
        self.big = []                      # (idx, feature, {shape})
        self._bodies = {}                  # id(feature) -> bodies_of
        for idx, f in self.feats:
            try:
                lst, whole = _feat_faces(f)
            except Exception:
                continue
            self.faces[id(f)] = lst
            if whole:
                for fc in lst:
                    try:
                        self.by_tid.setdefault(fc.tempId, []).append((fc, idx, f))
                    except Exception:
                        pass
            else:
                sigs = set()
                for fc in lst:
                    try:
                        sigs.add(_face_sig(fc))
                    except Exception:
                        pass
                self.big.append((idx, f, sigs))

    def owners(self, face, skip=()):
        try:
            hits = self.by_tid.get(face.tempId, [])
        except Exception:
            return []
        mine = [(idx, f) for fc, idx, f in hits if _same_body(fc, face)]
        if mine or not self.big:
            # Claimed by a small feature — even one that's skipped (a mirror's copy):
            # then no big feature made it.
            return [(idx, f) for idx, f in mine if not any(f is g for g in skip)]
        try:
            body, sig = face.body, _face_sig(face)
        except Exception:
            return []
        big = [(idx, f, sigs) for idx, f, sigs in self.big
               if not any(f is g for g in skip)
               and any(_same_body(body, b) for b in self.bodies_of(f))]
        hit = [(idx, f) for idx, f, sigs in big if sig in sigs]
        if hit or not big:
            return hit
        idx, f, _ = max(big, key=lambda t: t[0])
        return [(idx, f)]

    def maker(self, edge, skip=()):
        """The latest owner of the edge's faces — the feature that made the edge —
        leaving out `skip` BEFORE picking the latest: a Mirror claims the faces it
        copies, and it must not out-date the feature that really made the edge."""
        own = [h for fc in _coll(edge.faces) for h in self.owners(fc, skip)]
        if not own:
            return []
        top = max(i for i, _ in own)
        return [h for h in own if h[0] == top]

    def bodies_of(self, f):
        """phases._bodies_of, read once per feature per index: it was re-read for every
        face of every edge a big feature's walk touched — 5 s of a row click."""
        got = self._bodies.get(id(f))
        if got is None:
            got = self._bodies[id(f)] = phases._bodies_of(f)
        return got

    def faces_of(self, f):
        got = self.faces.get(id(f))
        if got is None:
            try:
                got = _feat_faces(f)[0]
            except Exception:
                got = []
        return got

    def still_valid(self):
        """Is what was read still the model? A recompute — an edit, a suppress, an
        undo — replaces the faces of every body it touches, and the old objects go
        invalid; hiding something, activating a component or opening a sketch replaces
        nothing. One face per feature is enough to tell."""
        try:
            for got in self.faces.values():
                if got and not got[0].isValid:
                    return False
            return all(f.isValid for _, f in self.feats)
        except Exception:
            return False


# Built once per component per model state. The state is the timeline's shape plus
# whether the faces read are still valid (an edit keeps the count but replaces faces) —
# NOT the last command: building this reads every feature's faces, seconds on a big
# component, and "any command finished" threw it away after every visibility toggle.
_face_idx_cache = {'key': None, 'idx': None}


def _face_index(design, comp):
    tl = design.timeline
    key = (_doc_key(), tl.count, tl.markerPosition, comp.id)
    c = _face_idx_cache
    if c['key'] != key or not c['idx'].still_valid():
        c.update(key=None, idx=_FaceIndex(design, comp))
        c['key'] = key
    return c['idx']


# Features whose faces are COPIES of earlier ones (patterns, mirror, move).
_COPY_TYPES = frozenset('adsk::fusion::' + t for t in (
    'RectangularPatternFeature', 'CircularPatternFeature', 'PathPatternFeature',
    'MirrorFeature', 'MoveFeature'))


def _face_sig(fc):
    return (fc.geometry.surfaceType, '%.5g' % fc.area)


def _input_sketch(design, feat, depth=0):
    """The sketch behind what a pattern / mirror / move COPIED: a patterned cut → the
    cut's sketch, not whatever the copies happen to touch. .inputEntities would say,
    but Fusion only answers it with the timeline rolled back to the feature ("Didn't
    roll editing feature back") — too heavy for a click. So the originals are found by
    shape: a copy has its original's surface type and area. The earlier feature most of
    whose faces the copies match (latest on a tie) is the input; sketchless, what IT
    copied, in turn."""
    if depth > 4 or feat.objectType not in _COPY_TYPES:
        return None
    try:
        own = _tl_index(feat.timelineObject)
        fi = _face_index(design, feat.parentComponent)
        mine = fi.faces_of(feat)
        sigs = {_face_sig(fc) for fc in mine}
        # A big copy is known only by a sample of its shapes: then ask how many of
        # THOSE the original holds, not how many of the original's are among them.
        sampled = len(mine) == _BIG_SAMPLE and feat.faces.count > _BIG
    except Exception:
        return None
    # Latest first; a full match that leads to a sketch ends the search there.
    scored = []
    for idx, g in sorted(fi.feats, key=lambda t: -t[0]):
        if idx >= own:
            continue
        try:
            gf = fi.faces_of(g)
            if not gf:
                continue
            gs = [_face_sig(fc) for fc in gf]
            frac = (len(sigs & set(gs)) / len(sigs) if sampled
                    else sum(x in sigs for x in gs) / len(gs))
        except Exception:
            continue
        if frac >= 0.999:
            sk = _source_sketch(g) or _input_sketch(design, g, depth + 1)
            if sk is not None:
                return sk
        elif frac >= 0.5:
            scored.append((frac, idx, g))
    for _, _, g in sorted(scored, key=lambda t: (-t[0], -t[1])):
        sk = _source_sketch(g) or _input_sketch(design, g, depth + 1)
        if sk is not None:
            return sk
    return None


_MIRROR = 'adsk::fusion::MirrorFeature'


def _mirror_maker(fi, ent, crossed=()):
    """The Mirror feature that made a face / edge / vertex — when, with mirrors NOT
    skipped (bar those already `crossed`), a mirror is what the trace would pick first
    — else None.
    A face counts as the mirror's only when nothing BUT mirrors claims it: a face that
    spans the plane (the plate a mirrored boss sits on) is the mirror's and the
    original's at once, and an edge on the ORIGINAL side bordering it must not read as
    mirrored."""
    def own_face(fc):
        hits = fi.owners(fc, crossed)
        real = [h for h in hits if h[1].objectType != _MIRROR]
        return real or hits

    def own_edge(e):
        hits = [h for fc in _coll(e.faces) for h in own_face(fc)]
        if not hits:
            return []
        top = max(i for i, _ in hits)
        return [h for h in hits if h[0] == top]

    if isinstance(ent, adsk.fusion.BRepFace):
        own = own_face(ent)
    elif isinstance(ent, adsk.fusion.BRepEdge):
        own = own_edge(ent)
    else:
        own = [h for e in _coll(ent.edges) for h in own_edge(e)]
    if not own:
        return None
    top = max(i for i, _ in own)
    return next((f for i, f in own if i == top and f.objectType == _MIRROR), None)


def _reflect(p, pl):
    n = pl.normal.copy()
    n.normalize()
    o = pl.origin
    d = 2 * ((p.x - o.x) * n.x + (p.y - o.y) * n.y + (p.z - o.z) * n.z)
    return adsk.core.Point3D.create(p.x - d * n.x, p.y - d * n.y, p.z - d * n.z)


_mirror_planes = {}                      # (face index key, mirror token) -> Plane | None


def _mirror_plane(fi, m):
    """The plane Mirror *m* reflects through. MirrorFeature.mirrorPlane only answers
    with the timeline rolled back to it (InternalValidationError otherwise — verified
    live 2026-09-24), so it's worked out from the geometry: each of the mirror's faces
    is paired with every earlier face of the same shape (_face_sig), each pair proposes
    the plane bisecting their centroids, and the plane most pairs agree on wins. A face
    lying ON the plane (its centroid its own copy's) proposes nothing.
    Only the mirror's real COPIES vote — faces nothing earlier claims. A face spanning
    the plane is the original's too, and pairs with its look-alikes elsewhere: a thin
    wall's two sides are the same area, and voted for the plane through the middle of
    its thickness, out-voting the real one (a sheet mirrored end to end, 2026-09-25).
    They vote only when no copy does.
    The ballot only proposes: every plane is then CHECKED (_best_plane) — a big mirror
    is known by a handful of sampled faces, paired against sampled originals, and its
    top vote can be a skewed plane nothing reflects through (Mirror - Depth, 804 faces:
    a plane through (10, 5, -1), 2026-09-29). The origin planes stand too, YZ first:
    parts are built in +X and mirrored to -X."""
    try:
        key = (_face_idx_cache['key'], m.entityToken)
    except Exception:
        # A mirror object that has gone stale under us (one the Mirror add-in's preview
        # made and rolled back is the usual one): no plane, rather than taking the
        # whole row click down with it.
        return None
    if key in _mirror_planes:
        return _mirror_planes[key]
    if len(_mirror_planes) > 32:
        _mirror_planes.clear()
    pl = None
    try:
        own = _tl_index(m.timelineObject)
        by_sig = {}
        for idx, g in fi.feats:
            if idx >= own or g.objectType == _MIRROR:
                continue
            for fc in fi.faces_of(g):
                try:
                    by_sig.setdefault(_face_sig(fc), []).append(fc.centroid)
                except Exception:
                    pass
        copies, shared = [], []
        for fc in fi.faces_of(m):
            try:
                orig = any(i < own and g.objectType != _MIRROR for i, g in fi.owners(fc))
            except Exception:
                orig = False
            (shared if orig else copies).append(fc)
        votes = _plane_votes(copies, by_sig) or _plane_votes(shared, by_sig)
        voted = [adsk.core.Plane.create(adsk.core.Point3D.create(*mid),
                                        adsk.core.Vector3D.create(*n))
                 for _, n, mid in sorted(votes.values(), key=lambda t: -t[0])[:3]]
        pl = _best_plane((copies + shared)[:8], voted)
    except Exception:
        pl = None
    _mirror_planes[key] = pl
    return pl


def _origin_planes():
    """The component's origin planes in its own space — YZ first (+X mirrored to -X)."""
    o = adsk.core.Point3D.create(0, 0, 0)
    return [adsk.core.Plane.create(o, adsk.core.Vector3D.create(*n))
            for n in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]


def _best_plane(faces, voted):
    """Of the origin planes and the `voted` ones (best first), the plane that reflects
    the most of `faces` onto another face of the same shape (_face_sig) — the origin
    planes win a tie. None of them landing: the top vote, as before, else None."""
    best, top = None, 0
    kind = adsk.fusion.BRepEntityTypes.BRepFaceEntityType
    for pl in _origin_planes() + voted:
        score = 0
        for fc in faces:
            try:
                comp = fc.body.parentComponent
                sig = _face_sig(fc)
                found = comp.findBRepUsingPoint(_reflect(fc.pointOnFace, pl), kind,
                                                0.001, False)
                if any(getattr(h, 'assemblyContext', None) is None
                       and not (h.tempId == fc.tempId and _same_body(h, fc))
                       and _face_sig(h) == sig for h in _coll(found)):
                    score += 1
            except Exception:
                pass
        if score > top:
            best, top = pl, score
    return best or (voted[0] if voted else None)


def _plane_votes(faces, by_sig):
    """_mirror_plane's ballot: {rounded plane: [votes, normal, point]} for `faces`
    paired with every earlier face of their shape (`by_sig`)."""
    votes = {}
    for fc in faces:
        try:
            c, cands = fc.centroid, by_sig.get(_face_sig(fc), ())
        except Exception:
            continue
        for o in cands:
            v = (c.x - o.x, c.y - o.y, c.z - o.z)
            ln = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
            if ln < 1e-5:
                continue
            n = tuple(x / ln for x in v)
            if next(x for x in n if abs(x) > 1e-9) < 0:
                n = tuple(-x for x in n)
            mid = ((c.x + o.x) / 2, (c.y + o.y) / 2, (c.z + o.z) / 2)
            d = sum(a * b for a, b in zip(n, mid))
            k = tuple(round(x, 3) for x in n) + (round(d, 3),)
            hit = votes.setdefault(k, [0, n, mid])
            hit[0] += 1
    return votes


def _mirror_twin(fi, ent, crossed=()):
    """The original a mirrored face / edge / vertex is a copy of: a point on it
    reflected through the mirror's plane (_mirror_plane), and the entity of the same
    kind at that point in the same component (a face of the same shape first). None
    when a mirror didn't make it, or its other side can't be found — or is itself (a
    face across the plane). Everything is native, so in component space.
    (mirror, twin), or (None, None)."""
    m = _mirror_maker(fi, ent, crossed)
    if m is None:
        return None, None
    try:
        pl = _mirror_plane(fi, m)
        if pl is None:
            return None, None
        comp = ent.body.parentComponent
        if isinstance(ent, adsk.fusion.BRepFace):
            p, kind = ent.pointOnFace, adsk.fusion.BRepEntityTypes.BRepFaceEntityType
        elif isinstance(ent, adsk.fusion.BRepEdge):
            p, kind = ent.pointOnEdge, adsk.fusion.BRepEntityTypes.BRepEdgeEntityType
        else:
            p, kind = ent.geometry, adsk.fusion.BRepEntityTypes.BRepVertexEntityType
        found = comp.findBRepUsingPoint(_reflect(p, pl), kind, 0.001, False)
    except Exception:
        return None, None
    hits = []
    for h in _coll(found):
        try:
            if getattr(h, 'assemblyContext', None) is not None \
                    or h.body.parentComponent.id != comp.id:
                continue
            if h.tempId == ent.tempId and _same_body(h, ent):
                continue
            hits.append(h)
        except Exception:
            pass
    if isinstance(ent, adsk.fusion.BRepFace) and len(hits) > 1:
        try:
            sig = _face_sig(ent)
            hits.sort(key=lambda h: _face_sig(h) != sig)
        except Exception:
            pass
    return m, (hits[0] if hits else None)


def _trace_candidates(design, ent, body, crossed=(), reflect=True):
    """(the features behind the pick, latest first; whether anything claimed it at all
    before the never-matched ones were dropped).
    A face / edge / vertex a Mirror made is traced on its OTHER side (_mirror_twin), so
    both halves of a mirrored thing land on the feature that really made them — and a
    mirror of a mirror follows through again. A mirror once crossed is skipped on the
    far side: a face spanning the plane is the mirror's on BOTH sides, and would bounce
    the trace straight back. Only when that side can't be found does it fall back to
    skipping the mirror.
    Still nothing — the mirror unrecognised, or its plane not found — and the part's
    reflection through an origin plane (_origin_twins, YZ first) is traced instead:
    only then, and only once (`reflect`)."""
    out, claimed = _trace_direct(design, ent, body, crossed)
    if out or not reflect or isinstance(ent, adsk.fusion.BRepBody):
        return out, claimed
    for twin in _origin_twins(ent):
        got = _trace_candidates(design, twin, twin.body, reflect=False)
        if got[0]:
            return got
    return out, claimed


def _origin_twins(ent):
    """The face / edge / vertex at `ent`'s reflection through each origin plane (YZ
    first) — the same shape (a face's _face_sig, an edge's length), not `ent` itself."""
    comp = ent.body.parentComponent
    if isinstance(ent, adsk.fusion.BRepFace):
        p, kind = ent.pointOnFace, adsk.fusion.BRepEntityTypes.BRepFaceEntityType
        same = lambda h: _face_sig(h) == _face_sig(ent)
    elif isinstance(ent, adsk.fusion.BRepEdge):
        p, kind = ent.pointOnEdge, adsk.fusion.BRepEntityTypes.BRepEdgeEntityType
        same = lambda h: abs(h.length - ent.length) < 1e-5
    else:
        p, kind = ent.geometry, adsk.fusion.BRepEntityTypes.BRepVertexEntityType
        same = lambda h: True
    for pl in _origin_planes():
        try:
            found = comp.findBRepUsingPoint(_reflect(p, pl), kind, 0.001, False)
        except Exception:
            continue
        for h in _coll(found):
            try:
                if getattr(h, 'assemblyContext', None) is None \
                        and h.body.parentComponent.id == comp.id \
                        and not (h.tempId == ent.tempId and _same_body(h, ent)) \
                        and same(h):
                    yield h
                    break
            except Exception:
                pass


def _trace_direct(design, ent, body, crossed=()):
    """_trace_candidates without the origin-plane fallback."""
    fi = _face_index(design, body.parentComponent)
    if len(crossed) < 4 and not isinstance(ent, adsk.fusion.BRepBody):
        m, twin = _mirror_twin(fi, ent, crossed)
        if twin is not None:
            got = _trace_direct(design, twin, twin.body, tuple(crossed) + (m,))
            if got[0]:
                return got
    touching = [(idx, f) for idx, f in fi.feats
                if any(_same_body(body, b) for b in fi.bodies_of(f))]
    skip = [f for _, f in fi.feats if f.objectType == _MIRROR]
    hits = []
    if not isinstance(ent, adsk.fusion.BRepBody):
        if isinstance(ent, adsk.fusion.BRepFace):
            hits = fi.owners(ent, skip)
        elif isinstance(ent, adsk.fusion.BRepEdge):
            hits = fi.maker(ent, skip)
        else:
            hits = [h for e in _coll(ent.edges) for h in fi.maker(e, skip)]
        claimed = bool(hits) or any(fi.owners(fc) for fc in (
            [ent] if isinstance(ent, adsk.fusion.BRepFace) else _coll(ent.faces)))
    else:
        claimed = bool(touching)
    direct = claimed and not isinstance(ent, adsk.fusion.BRepBody)
    found = {}
    for idx, f in hits if direct else touching:
        if any(f is g for g in skip):       # every hit is one of fi.feats' own objects
            continue
        found[id(f)] = (idx, f)
    out = [f for _, f in sorted(found.values(), key=lambda t: -t[0])]
    # The body's first feature (what made it — the top of its feature rows, one click
    # away) only when it's the sole answer — but only on the body-wide list (a body
    # pick, or a pick nothing claimed). A face / edge / vertex it MADE keeps it.
    first = min(touching, key=lambda t: t[0])[1] if touching else None
    if not direct and len(out) > 1 and first is not None:
        out = [f for f in out if f is not first]
    return out, claimed


# More than this many edges (mirror copies included) and none are added — just the
# feature and its sketch: selecting edges lags, and a pattern's thousands would stall
# the press for seconds.
_MAX_EDGES = 100


def _made_edges(design, tok):
    """The edges the feature `tok` MADE, by the same rule an edge pick traces with
    (_edge_owner): an edge is its feature's when that feature is the latest owner of
    its faces, a mirror's copy its original's — so each edge has exactly one feature. A small feature's edges all border its own faces, so only those are
    walked; a big one (known by a sample only) walks every edge of its bodies."""
    f = _find(design, tok)
    if f is None:
        return []
    fi = _face_index(design, f.parentComponent)
    # By ==, not by reading every feature's entityToken (slow, and it leaks).
    mine = next((g for _, g in fi.feats if g == f), None)
    if mine is None:
        return []
    skip = [g for _, g in fi.feats if g.objectType == _MIRROR]
    if any(g is mine for _, g, _s in fi.big):
        # More faces than the edge cap: it made more edges than that too, and the walk
        # below would read every edge of its bodies (4,600 on a rib-grid cut, half a
        # second a click) only to give up. Its face count is one cheap call.
        try:
            if mine.faces.count > _MAX_EDGES:
                return []
        except Exception:
            pass
        edges = [e for b in fi.bodies_of(mine) for e in _coll(b.edges)]
    else:
        edges = [e for fc in fi.faces_of(mine) for e in _coll(fc.edges)]
    seen, out = {}, []
    for e in edges:
        try:
            twins = seen.setdefault(e.tempId, [])
            if any(_same_body(e, t) for t in twins):
                continue
            twins.append(e)
            if any(g is mine for _, g in fi.maker(e, skip)):
                out.append(e)
        except Exception:
            pass
        if len(out) > _MAX_EDGES:
            return []
    # One feature per edge — the latest that would select it: an edge a later feature
    # claims (through a mirror too) is that feature's, never this one's.
    keep = []
    for e in _mirrored_edges(fi, mine, out):
        try:
            if _edge_owner(fi, e, skip) is mine:
                keep.append(e)
        except Exception:
            pass
    return keep


def _mirrored_edges(fi, feat, edges):
    """`edges` plus their copies by every Mirror later than `feat`, in timeline order —
    so a copy a later mirror mirrors again comes too. A copy is the edge at the
    reflected point (_reflect through _mirror_plane) that the mirror made
    (_mirror_maker) and is as long; an edge lying on the plane is its own copy."""
    try:
        own = _tl_index(feat.timelineObject)
    except Exception:
        return edges
    out = list(edges)
    kind = adsk.fusion.BRepEntityTypes.BRepEdgeEntityType
    for idx, m in sorted(fi.feats, key=lambda t: t[0]):
        if idx <= own or m.objectType != _MIRROR:
            continue
        pl = _mirror_plane(fi, m)
        if pl is None:
            continue
        for e in list(out):
            try:
                comp = e.body.parentComponent
                found = comp.findBRepUsingPoint(_reflect(e.pointOnEdge, pl), kind,
                                                0.001, False)
            except Exception:
                continue
            for h in _coll(found):
                try:
                    if getattr(h, 'assemblyContext', None) is not None \
                            or abs(h.length - e.length) > 1e-5 \
                            or any(h.tempId == x.tempId and _same_body(h, x) for x in out) \
                            or _mirror_maker(fi, h) is not m:
                        continue
                    out.append(h)
                    break
                except Exception:
                    pass
            if len(out) > _MAX_EDGES:
                return []
    return out


def _select_made_edges(design, tok, occ_tok):
    occ = _find(design, occ_tok) if occ_tok else None
    sel = ui.activeSelections
    for e in _made_edges(design, tok):
        try:
            sel.add(e.createForAssemblyContext(occ) if occ is not None else e)
        except Exception:
            pass


def _edge_owner(fi, e, skip, crossed=()):
    """The ONE feature an edge belongs to, as an edge pick traces it (_trace_candidates):
    a mirror's copy → its original's owner; else the latest feature among its makers.
    None when nothing claims it."""
    if len(crossed) < 4:
        m, twin = _mirror_twin(fi, e, crossed)
        if twin is not None:
            got = _edge_owner(fi, twin, skip, tuple(crossed) + (m,))
            if got is not None:
                return got
    hits = fi.maker(e, skip)
    return max(hits, key=lambda t: t[0])[1] if hits else None


def _trace_miss(claimed):
    return ("Only a mirror made that, and its other side wasn't found."
            if claimed else 'No timeline feature made that.')


def _trace_pick(design):
    t = _trace
    sel = _sel_entities()
    if not sel:
        return      # nothing picked: do nothing — no HUD
    if t['cands'] and _same_sel(t['sel'], sel):
        # The second press edits what the first found, as the Browser's second Edit
        # press does (_edit_traced) — no stepping back through the others.
        _edit(design, *t['cands'][t['i']], False)
        return
    else:
        t.update(cands=[], sel=None)
        # A feature already selected — a row click, Fusion's own timeline, a trace from
        # before a reload: that's what the first press would have found, so edit it.
        feat = _selected_feature(sel)
        if feat is not None:
            _edit(design, *feat, False)
            return
        ent, body, occ = _pick(sel[-1]) if sel else (None, None, None)
        if body is None:
            sk, occ = _picked_sketch(sel)
            if sk is None:
                _hud('fail', 'Pick a face, edge, body or sketch first.')
                return
            f = _sketch_feature(design, sk)
            if f is None:
                _hud('fail', 'No timeline feature was made from %s.'
                     % (getattr(sk, 'name', '') or 'that sketch'))
                return
            feats, claimed = [f], False
        else:
            feats, claimed = _trace_candidates(design, ent, body)
        cands = []
        for f in feats:
            try:
                same = occ is not None and occ.component.id == f.parentComponent.id
                cands.append((f.entityToken, occ.entityToken if same else ''))
            except Exception:
                pass
        if not cands:
            _hud('fail', _trace_miss(claimed))
            return
        t.update(cands=cands, i=0)
    tok, occ_tok = t['cands'][t['i']]
    _select_feature(design, tok, occ_tok)
    _select_made_edges(design, tok, occ_tok)
    t['sel'] = _sel_entities()
    quiet = _DOCK.get('quiet_select')
    if quiet:
        try:
            quiet([tok])
        except Exception:
            pass


def _picked_sketch(sel):
    """(native sketch, occurrence) for the last pick that is a sketch or part of one
    (curve, point, profile, text — anything with a parentSketch), else (None, None)."""
    for e in reversed(sel):
        try:
            occ = getattr(e, 'assemblyContext', None)
            nat = getattr(e, 'nativeObject', None) or e
            sk = adsk.fusion.Sketch.cast(nat) or getattr(nat, 'parentSketch', None)
            if sk is not None:
                return (getattr(sk, 'nativeObject', None) or sk), occ
        except Exception:
            continue
    return None, None


def _sketch_feature(design, sk):
    """The earliest live timeline feature made from sketch *sk* (_source_sketch), walking
    the timeline from the sketch on and stopping at the first: its own component's
    features first, then any other component's (a feature can take a profile across
    components). A Flange (found only by shape, slow) is tried only when nothing else
    is. A hit is quick; a miss reads every later feature (~2 s on the 365-item doc)."""
    try:
        after = _tl_index(sk.timelineObject)
    except Exception:
        after = -1
    own = sk.parentComponent.id
    later = []
    for o in _timeline_objects(design.timeline):
        try:
            # < not <=: in a collapsed group every item shares the group's slot
            if _tl_index(o) < after or o.isRolledBack or o.isSuppressed:
                continue
            e = o.entity
            if e is not None and e.objectType.endswith('Feature'):
                later.append(e)
        except Exception:
            pass
    def made(f, find):
        # A feature's inputs come back as proxies (Profile - X via its occurrence), so
        # compare native sketches — a proxy never == its native (seen live 2026-09-30).
        try:
            s = find(f)
            return s is not None and (s.nativeObject or s) == sk
        except Exception:
            return False

    rest, opaque = [], []
    for f in later:
        try:
            if f.objectType in _OPAQUE_SKETCH_TYPES:
                opaque.append(f)
            elif f.parentComponent.id != own:
                rest.append(f)
            elif made(f, _source_sketch):
                return f
        except Exception:
            pass
    for f in rest:
        if made(f, _source_sketch):
            return f
    for f in opaque:
        if made(f, _sketch_on_body):
            return f
    return None


def _trace_first(ent):
    """dock['trace_first'](picked face / edge / vertex): the token of the feature Trace
    would pick first for it, '' for none (or a whole body). The Browser selects that
    feature's row when a viewport pick opens its body — the viewport keeps the pick."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    e, body, _occ = _pick(ent)
    if design is None or body is None or e is body:
        return ''
    try:
        feats, _claimed = _trace_candidates(design, e, body)
        return feats[0].entityToken if feats else ''
    except Exception:
        return ''


def _feat_probe(design, mod, pid, toks, targets):
    """The Browser's drag: for each target token, may the dragged features go just
    BEFORE it (`ok`) / just AFTER it (`after`)? Same phase rule as the panel's."""
    why = _move_blocked(design)
    objs = None if why else _movers(design, toks)
    if not why and objs is None:
        why = "That's inside a collapsed Fusion group — expand it in Fusion first."
    ok, after = [], []
    if objs:
        tl = design.timeline
        for tok in targets or []:
            t = _tl_obj(design, tok)
            if t is None:
                continue
            for idx, acc in ((t[1], ok), (t[1] + 1, after)):
                i = -1 if idx >= tl.count else idx
                if phases.slot_ok(tl, objs, i) and _can_go(tl, objs, i):
                    acc.append(tok)
    mod.patch_feats(probe={'id': pid, 'ok': ok, 'after': after, 'why': why})


def _feat_reorder(design, mod, toks, before, after):
    """Move `toks` (kept in timeline order) just before `before` or just after `after`.
    Returns what was said about it — '' when it went where it was dropped. The Browser
    draws the rows there while this runs; a non-empty answer puts them back, in red."""
    said = []

    def fail(text):
        said.append(text)
        _notice(mod, text)

    why = _move_blocked(design)
    objs = None if why else _movers(design, toks)
    if why or objs is None:
        fail(why or "Can't move that — it's inside a collapsed Fusion group.")
        return said[-1]
    tl = design.timeline
    anchor = _tl_obj(design, before or after)
    if anchor is None:
        fail("Can't drop there.")
        return said[-1]
    idx = anchor[1] + (1 if after else 0)
    idx = -1 if idx >= tl.count else idx
    if not phases.slot_ok(tl, objs, idx):
        fail(_PHASE_LINE)
        return said[-1]
    if not _can_all(objs, idx):
        if _later(tl, objs, idx):
            _carry(design, objs, idx, fail)
            try:
                # The row dropped after depends on the movers itself: it came along,
                # so it's still after them — that order can't exist.
                if not said and after and anchor[0].index > objs[-1].index:
                    fail('%s depends on it, so it has to stay after it.' % anchor[0].name)
            except Exception:
                pass
            return said[-1] if said else ''
        fail("Fusion won't allow that order (a dependency would break).")
        return said[-1]
    prev = anchor[0]
    for n, o in enumerate(objs):
        try:
            if after:
                i = prev.index + 1
                i = -1 if i >= tl.count else i
            else:
                i = anchor[0].index
            moved = o.canReorder(i) and o.reorder(i)
        except Exception:
            moved = False
        if not moved:
            fail('Moved %d of %d — Fusion refused the rest.' % (n, len(objs)))
            return said[-1]
        prev = o
    return ''


def _set_phase(design, mod, tok, phase):
    ent = _find(design, tok)
    if ent is None:
        return
    try:
        o = ent.timelineObject
    except Exception:
        return _notice(mod, 'That has no timeline item.')
    p = int(phase) if phase else None
    if p is not None and not 1 <= p <= 7:
        return
    phases.set_override(ent, p)
    why = _move_blocked(design)
    if why:
        return _notice(mod, why)
    note = phases.replace(design, o)
    if note:
        _notice(mod, note)


def _sort_phases(design, mod):
    why = _move_blocked(design)
    if why:
        return _notice(mod, why)
    moved, notes = phases.sort_all(design)
    head = 'Sorted: moved %d item%s.' % (moved, '' if moved == 1 else 's')
    _notice(mod, ' '.join([head] + notes))


def _apply_commands(design, mod):
    for cmd in mod.pop_commands():
        try:
            a = cmd.get('action')
            if a == 'toggle':
                _toggle_mode()
                continue
            if a == 'setmode':
                _set_mode(str(cmd.get('mode') or ''))
                continue
            if a == 'watch':
                _feats_state['watch'] = [w for w in (cmd.get('comps') or [])
                                         if isinstance(w, dict)]
                _feats_state['dirty'] = True
                continue
            if not design:
                continue
            with tickguard.hold('timeline'):
                if a == 'rename':
                    _rename(design, cmd.get('tok'), str(cmd.get('name') or '').strip())
                elif a == 'renamecomp':
                    _rename_comp(design, cmd.get('ref'), str(cmd.get('name') or '').strip())
                elif a == 'edit':
                    _edit(design, cmd.get('tok'), cmd.get('occTok') or '', bool(cmd.get('sketch')))
                elif a == 'editsource':
                    _edit_source(design, cmd.get('tok'), cmd.get('occTok') or '')
                elif a == 'edittraced':
                    _edit_traced(design, cmd.get('tok'))
                elif a == 'rollto':
                    _roll_to(design, mod, cmd.get('tok'))
                elif a == 'suppress':
                    _suppress(design, cmd.get('toks') or [], cmd.get('on'))
                elif a == 'delete':
                    _delete(design, cmd.get('toks') or [])
                elif a == 'probe':
                    _probe(design, mod, cmd.get('id'), cmd.get('toks') or [],
                           cmd.get('targets') or [])
                    continue                     # asks, changes nothing: no re-walk
                elif a == 'reorder':
                    _reorder(design, mod, cmd.get('toks') or [], str(cmd.get('before') or ''))
                elif a == 'selectfeature':
                    # A row click selects what Trace would: the feature, its sketch and
                    # the edges it made. Not a Mirror's — that selection is cleared.
                    tok, occ_tok = cmd.get('tok'), cmd.get('occTok') or ''
                    _select_feature(design, tok, occ_tok)
                    ent = _find(design, tok) if tok else None
                    is_mirror = bool(ent) and ent.objectType == 'adsk::fusion::MirrorFeature'
                    if ent and not is_mirror:
                        try:
                            _select_made_edges(design, tok, occ_tok)
                        except Exception:
                            mod.set_debug(last_error=traceback.format_exc())
                    # A mirror leaves the viewport EMPTY (see _select_feature), so Edit
                    # Feature has nothing to act on there. Held here instead — see
                    # _held_mirror.
                    _held_mirror.update(tok=tok if is_mirror else None, occ=occ_tok,
                                        at=time.time())
                    # Counts as Trace's first press: the next one edits it (a Flange
                    # isn't in the selection itself for _selected_feature to find).
                    _trace.update(cands=[(tok, occ_tok)], i=0, sel=_sel_entities())
                    # And the Browser is told it is this feature, as after a Trace —
                    # otherwise its _sel_pick sees an edge picked last and traces it
                    # back through the face index to the feature it already knows.
                    quiet = _DOCK.get('quiet_select')
                    if quiet and tok:
                        try:
                            quiet([tok])
                        except Exception:
                            pass
                    continue                     # selection only: nothing to re-walk
                elif a == 'trace':
                    # The Browser's Go In / Edit on a viewport part pick: the first press
                    # is Select Feature Behind Pick; the next edits its sketch (Go In,
                    # editsource) or the feature itself (Edit, edittraced).
                    _trace_pick(design)
                    continue
                elif a == 'featprobe':
                    _feat_probe(design, mod, cmd.get('id'), cmd.get('toks') or [],
                                cmd.get('targets') or [])
                    continue
                elif a == 'featreorder':
                    # The answer rides out with the next /feats walk (_maybe_feats), so
                    # the page sees the verdict and the rows it produced together.
                    rid = cmd.get('id')
                    _feats_state['reorder'] = {'id': rid, 'text': "Fusion hit an error making that move."}
                    _feats_state['dirty'] = True
                    text = _feat_reorder(design, mod, cmd.get('toks') or [],
                                         str(cmd.get('before') or ''), str(cmd.get('after') or ''))
                    _feats_state['reorder'] = {'id': rid, 'text': text or ''}
                elif a == 'setphase':
                    _set_phase(design, mod, cmd.get('tok'), cmd.get('phase'))
                elif a == 'sortphases':
                    _sort_phases(design, mod)
                elif a == 'deselect':
                    # A clears BOTH sides, as in the Browser: pressed in the viewport the
                    # Browser add-in vetoes deselectAllCmd and hands it to this page.
                    ui.activeSelections.clear()
                elif a == 'command':
                    cd = ui.commandDefinitions.itemById(str(cmd.get('cmd_id') or ''))
                    if cd:
                        cd.execute()
            _sig['last'] = None                  # show the result on the next tick
            _feats_state['dirty'] = True
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())


# ── A mirror picked in the Browser ────────────────────────────────────────────
# Clicking a Mirror row selects NOTHING in the viewport (_select_feature: selecting a
# mirror lights up its whole body). So an Edit Feature pressed next — Fusion's own, or the
# per-type one, from a key or the menu — would start with an empty selection and edit
# nothing. While that row is the last thing picked and the viewport is still empty, an
# edit command is refused at commandStarting and this mirror is edited instead, on the
# next tick (never from inside commandStarting). _edit selects the mirror first, so its
# own FusionDcMirrorPatternEditCommand passes straight through here.
#
# Let go of on any viewport click after the pick (the click stamp FusionScrollFilter
# writes) — a click on empty canvas deselects the row in the Browser too.
_held_mirror = {'tok': None, 'occ': '', 'at': 0.0}
_pending_edit = {}
_EDIT_TAKEOVER_IDS = ('FusionEditFeatureCommand', 'FusionDcMirrorPatternEditCommand',
                      'FusionEditDcFeatureCommand')
_CLICK_STAMP = '/tmp/fusion_click_cmd'


def _held_mirror_live():
    if not _held_mirror.get('tok'):
        return False
    try:
        if ui.activeSelections.count:
            return False
    except Exception:
        return False
    try:
        if os.path.getmtime(_CLICK_STAMP) > _held_mirror['at']:
            _held_mirror['tok'] = None
            return False
    except OSError:
        pass
    return True


# ── Event handlers ────────────────────────────────────────────────────────────
class SyncHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _tick_pending
        _tick_pending = 0.0
        if not _active:
            return
        mod = _server_mod
        if mod is None:
            return
        with tickguard.tick('timeline') as go:
            if not go:
                return
            try:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if _pending_edit.get('tok') and design:
                    tok, occ = _pending_edit.pop('tok'), _pending_edit.pop('occ', '')
                    with tickguard.hold('timeline'):
                        _edit(design, tok, occ, False)
                _apply_commands(design, mod)
                # The lock holds only while the page is actually on screen: quit the
                # dock in timeline mode and Fusion is itself again (the mode is kept,
                # so reopening the dock brings it back). Fusion's own bar is left
                # alone — hiding it is the Hide Native Timeline add-in's job.
                live = _DOCK.get('mode') == 'timeline' and mod.window_alive(3.0)
                if live != bool(_DOCK.get('live')):
                    _DOCK['live'] = live
                mod.patch_state(locked=live and _DOCK.get('lockOcc') is not None)
                if design:
                    _place_tick(design, mod)
                    _sketch_groups_tick(design, mod)
                    _maybe_feats(design, mod)
                if _DOCK.get('mode') != 'timeline':
                    return
                if not design:
                    mod.update_state([], {'doc_id': '', 'comps': {}, 'activeRef': ''})
                    return
                _enforce_lock(design)
                if mod.window_alive():
                    _maybe_walk(design, mod)
            except Exception:
                mod.set_debug(last_error=traceback.format_exc())


class StartingHandler(adsk.core.ApplicationCommandEventHandler):
    """The toggle is refused as a command and done here (like the Browser's dock
    toggle: nothing to run, so nothing should end the command you were in); and while
    timeline mode is on, anything that would leave the locked component is refused."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cid = args.commandId
            if cid == TOGGLE_CMD_ID:
                args.isCanceled = True
                _toggle_mode()
                return
            if cid == TRACE_CMD_ID:
                args.isCanceled = True
                if _sketch_open():
                    # Space is Trace outside a sketch and Smart Dimension inside one,
                    # and remapd picks between the two surrogates off /tmp/fusion_sketch.
                    # That stamp can read 'unknown' (stale) while the command stamp is
                    # still fresh, and then fusion-idle wins and the Trace chord lands
                    # here mid-sketch. Fusion's own state is the authority, so hand the
                    # press on instead of tracing.
                    cd = ui.commandDefinitions.itemById(SMART_DIMENSION_CMD_ID)
                    if cd:
                        cd.execute()
                        return
                _run_trace()
                return
            if cid in _EDIT_TAKEOVER_IDS and _held_mirror_live():
                args.isCanceled = True
                _pending_edit.update(tok=_held_mirror['tok'], occ=_held_mirror['occ'])
                app.fireCustomEvent(EVENT_ID, '')
                return
            if (_DOCK.get('mode') != 'timeline' or not _DOCK.get('live')
                    or cid not in LOCK_CMD_IDS):
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design and _editing_sketch(design):
                return                        # Go Up is how a sketch is left
            args.isCanceled = True
            if cid in FORWARD_LOCKED:
                _push(cid)
        except Exception:
            if _server_mod:
                _server_mod.set_debug(last_error=traceback.format_exc())


class TerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """A finished command may have reordered, suppressed or renamed something the
    signature can't see — rebuild the walk's structure on the next tick. Selection and
    view navigation change nothing, so they don't pay for a rebuild."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cid = args.commandId or ''
            if cid == 'SelectCommand' or any(w in cid for w in _VIEW_WORDS):
                return
            _last_cmd.update(id=cid, at=time.monotonic())     # the placement's hint
            _feats_state['dirty'] = True
            if _DOCK.get('mode') == 'timeline':
                _struct['items'] = None
        except Exception:
            pass


_VIEW_WORDS = ('Orbit', 'Pan', 'Zoom', 'LookAt', 'ViewCube', 'Camera')


class ToggleCreatedHandler(adsk.core.CommandCreatedEventHandler):
    """Fallback only: the veto above normally stops the command before it is created."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = ToggleExecuteHandler()
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class ToggleExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _toggle_mode()


def _run_trace():
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    with tickguard.hold('timeline'):
        try:
            _trace_pick(design)
        except Exception:
            if _server_mod:
                _server_mod.set_debug(last_error=traceback.format_exc())


class TraceCreatedHandler(adsk.core.CommandCreatedEventHandler):
    """Fallback only, like ToggleCreatedHandler."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = TraceExecuteHandler()
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class TraceExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _run_trace()


# ── Lifecycle ─────────────────────────────────────────────────────────────────
def run(context):
    global _custom_evt, _active, _starting_handler, _terminated_handler
    try:
        _start_server()
        _DOCK['push'] = _push
        _DOCK['set_mode'] = _set_mode       # the Browser's `y` drops timeline mode
        _DOCK['trace_first'] = _trace_first
        _active = True
        _stop_evt.clear()
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = SyncHandler()
        _custom_evt.add(h)
        handlers.append(h)

        cd = ui.commandDefinitions.itemById(TOGGLE_CMD_ID)
        if not cd:
            cd = ui.commandDefinitions.addButtonDefinition(
                TOGGLE_CMD_ID, 'Dock: Timeline / Browser',
                'Swap the left dock between the component Browser and the vertical timeline.')
        hc = ToggleCreatedHandler()
        cd.commandCreated.add(hc)
        handlers.append(hc)

        cd = ui.commandDefinitions.itemById(TRACE_CMD_ID)
        if not cd:
            cd = ui.commandDefinitions.addButtonDefinition(
                TRACE_CMD_ID, 'Timeline: Select Feature Behind Pick',
                'Select the latest timeline feature that made the picked face, edge or '
                'body, with its sketch. Again: edit it.')
        hc = TraceCreatedHandler()
        cd.commandCreated.add(hc)
        handlers.append(hc)

        _starting_handler = StartingHandler()
        ui.commandStarting.add(_starting_handler)
        handlers.append(_starting_handler)

        _terminated_handler = TerminatedHandler()
        ui.commandTerminated.add(_terminated_handler)
        handlers.append(_terminated_handler)

        threading.Thread(target=_poller, daemon=True).start()
        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Fusion Timeline failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _custom_evt, _active, _starting_handler, _terminated_handler
    try:
        _active = False
        _stop_evt.set()
        # With this add-in gone nothing can feed the timeline or undo the lock, so the
        # dock goes back to the Browser.
        _DOCK.update(mode='browser', lockOcc=None, push=None, set_mode=None, live=False)
        if _starting_handler is not None:
            try:
                ui.commandStarting.remove(_starting_handler)
            except Exception:
                pass
            _starting_handler = None
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
    except Exception:
        pass
