"""Flush Undo — drop the active document's transaction (undo/redo) log without
restarting Fusion, after a general display/selection reset (_refresh).

SINCE 2026-09-27: A GENERAL REFRESH TOO
---------------------------------------
Before the trim, the manual flush runs _refresh: the known ways Fusion gets stuck
that a reset un-sticks without a restart — faces left undrawn after a feature
(recomputed by rolling the marker back and forward), a stale selection highlight
nothing can deselect, a leftover Smart Extrude ghost, no active command. Each is one step in
_refresh; add new ones there. The idle auto-trim does NOT run it (it would clear
your selection while you're away).

SINCE 2026-09-29: IN PLACE, BY Ns::TransactionMgr::trimUndo
------------------------------------------------------------
The in-place trim used to be the hidden `Transaction.Trim` text command, which is
just `asset->stopTransacting(); asset->startTransacting()`: it tears the document's
transaction system down and back up, and whatever draws NEW bodies never
re-attaches. Every body made after it was valid but never drawn, until Undo+Redo
or a reopen. _trim now calls the exported TransactionMgr::trimUndo() + trimRedo()
directly (see _NativeTrim): Fusion's own undo trim, which fires
preDocumentUndoTrimmed and clears the done/undone changes, with the transaction
system left running. Verified on a fresh document: same result in the listing,
Undo/Redo afterwards fine, new bodies drawn — and `Transaction.Trim` in the same
document then broke them, so the check does detect the bug. A document that the
old trim already broke stays broken until it's reopened.

2026-09-26: IN PLACE FIRST
--------------------------
Fusion can drop the chain in place (see _trim). That is now the flush; the
close/reopen below only runs when the trim can't (a command is open) or doesn't
take. And the history grows far slower now:
the visibility engine groups each jump into ONE undo step (Tick Guard.py § One undo
step), which also disproves "wrapping the writes does NOT collapse them" below —
a command doesn't, but `Transaction.Start`/`Commit` does.

WHY THIS EXISTS
---------------
Every visibility change that actually changes something is its own Fusion
transaction, and Fusion keeps every transaction for as long as the document
stays open.  Measured on a 33-component design:

  • ~22 KB retained per real light-bulb write (only ~6 KB on a tiny document —
    the cost scales with model size)
  • ~268 KB per jump-up-and-back, of which ~44 KB is component activation and
    the rest is the visibility writes
  • per-write cost climbing ~30% every 900 writes — which is the slowdown

Nothing suppresses it.  Wrapping the writes in a single command does NOT collapse
them (Fusion records per property set, not per command); undo does not reclaim
the memory (it moves the records onto the redo stack); Fusion's native Isolate is
cheaper per cycle but three times slower in wall-clock; and there is no undo-limit
setting in the preferences plist or in NGlobalOptions.xml.

The log belongs to the OPEN DOCUMENT, so closing and reopening the document is
what drops it.  That is all this add-in does: remember where you are, save if
dirty, close, reopen, and put you back — camera, workspace, active component and
isolation included.  Seconds, instead of a full Fusion restart.

WHAT A FLUSH COSTS
------------------
  • Undo/redo history for this document is gone — the same thing a restart costs
    you today, just on demand.
  • A modified document is saved first, which creates a version.  Closing without
    saving would throw the work away, so this is not optional.
  • Only the ACTIVE document is flushed; other open documents keep their logs.
"""

import adsk.core, adsk.fusion, traceback, os, sys, threading, time, datetime

# ── process-wide state ────────────────────────────────────────────────────────
#
# This module can legitimately exist TWICE in one Fusion process: once loaded by
# Fusion itself from Scripts and Add-Ins, and once exec'd into sys.modules to
# bootstrap it live (see the fusion_mcp_bridge notes).  Both copies register a
# handler on the same custom event, so a single hotkey press ran _do_flush twice
# concurrently — the second flush closed and reopened the document while the
# first was still inside doc.save(), and the first then closed a Document object
# that no longer existed:
#
#     "Flush Undo: could not close the document. 4 : An API Object refers to a
#      deleted Object"
#
# A module-level guard cannot see the other copy, so the busy flag, the pending
# timer and the previous instance's stop() all live in one dict hung off `sys`,
# which every copy in the process shares.
_STATE = sys.__dict__.setdefault(
    '__josh_flush_undo__', {'busy': False, 'timer': None, 'stop': None,
                             'awaiting_sketch_stop': False, 'pending_sketch': None})

import types as _types
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fvs = _load('fvs', 'Visibility Snapshot.py')

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

CMD_ID   = 'FlushUndoCmd'
CMD_NAME = 'Flush Undo'
CMD_DESC = ('Reset stuck highlights / selection / ghosts, then drop the active '
            "document's undo/redo transaction log (in place, or by close and reopen "
            'restoring camera, workspace, active component and isolation)')

TRIM_IN_PLACE = True   # False: always the close/reopen (see _trim)

# NEVER go back to the `Transaction.Trim` text command: it breaks the display of every
# body created afterwards in the document (see the docstring). _trim uses _NativeTrim.

# Automatic trim once you've been away from keyboard and mouse this long (seconds;
# None = never). A background thread only READS the idle time (CoreGraphics, no
# Fusion API) and fires the flush event; everything else happens on the main thread,
# so a hung / beachballing Fusion simply gets it later — and it re-checks you're still
# idle and that nothing is running (a command, a dialog) before trimming.
AUTO_TRIM_IDLE = 120
IDLE_POLL = 10

FLUSH_EVENT_ID = 'JoshFlushUndoEvent'
_flush_event   = None

LOG = '/tmp/fusion_flush_undo.log'


def _log(msg):
    try:
        ts = datetime.datetime.now().strftime('%H:%M:%S.%f')
        with open(LOG, 'a') as f:
            f.write(f'[{ts}] [FlushUndo] {msg}\n')
    except Exception:
        pass


# ── capture / restore ─────────────────────────────────────────────────────────

def _capture(doc, design):
    """Everything needed to put the user back exactly where they were."""
    state = {'urn': None, 'camera': None, 'workspace': None, 'occ_path': None,
             'datafile': None}

    df = doc.dataFile
    state['urn'] = df.id

    try:
        cam = app.activeViewport.camera
        state['camera'] = {
            'eye':      [cam.eye.x, cam.eye.y, cam.eye.z],
            'target':   [cam.target.x, cam.target.y, cam.target.z],
            'up':       [cam.upVector.x, cam.upVector.y, cam.upVector.z],
            'type':     cam.cameraType,
            'angle':    cam.perspectiveAngle,
            'extents':  cam.viewExtents,
        }
    except Exception:
        pass

    try:
        state['workspace'] = ui.activeWorkspace.id
    except Exception:
        pass

    # Where we are in the hierarchy.  fullPathName survives a close/reopen because
    # it is built from occurrence names, not tokens.
    try:
        occ = fvs._active_occ(design)
        state['occ_path'] = occ.fullPathName if occ else None
    except Exception:
        pass

    return state


def _restore(state):
    """Reopen the document and put everything back.  Returns a list of failures."""
    failed = []

    # Prefer the DataFile resolved BEFORE the close (see _do_flush): looking it up
    # here would spend a cloud round trip during the window where no document is
    # open, which is exactly when Fusion collapses its chrome back to the empty
    # state and Maximize Viewport has to re-hide it.  Shorter window, less flash.
    df = state.get('datafile')
    try:
        if df is not None:
            df.id  # cheap liveness probe; a dead handle raises
    except Exception:
        df = None
    if df is None:
        df = app.data.findFileById(state['urn'])
    if df is None:
        failed.append('the document is no longer in the cloud data')
        return failed

    try:
        app.documents.open(df, True)
    except Exception as e:
        failed.append('reopen: %s' % e)
        return failed

    # Workspace first — switching workspaces after restoring the camera would
    # undo the camera.
    ws = state.get('workspace')
    if ws:
        try:
            if ui.activeWorkspace.id != ws:
                w = ui.workspaces.itemById(ws)
                if w:
                    w.activate()
        except Exception as e:
            failed.append('workspace: %s' % e)

    design = adsk.fusion.Design.cast(app.activeProduct)

    # Active component + isolation, through the same single authority the
    # navigation add-ins use, so the result is identical to navigating there.
    path = state.get('occ_path')
    if design:
        try:
            target = None
            if path:
                for occ in design.rootComponent.allOccurrences:
                    if occ.fullPathName == path:
                        target = occ
                        break
            if target:
                target.activate()
                fvs.remember_activation(target)
                if fvs.isolate_is_active():
                    fvs.isolate_occurrence_no_snapshot(design, target)
            else:
                design.activateRootComponent()
                fvs.remember_activation(None)
            fvs.apply_policy(design)
        except Exception as e:
            failed.append('active component: %s' % e)

    cam = state.get('camera')
    if cam:
        try:
            vp = app.activeViewport
            c = vp.camera
            c.cameraType         = cam['type']
            c.eye                = adsk.core.Point3D.create(*cam['eye'])
            c.target             = adsk.core.Point3D.create(*cam['target'])
            c.upVector           = adsk.core.Vector3D.create(*cam['up'])
            c.perspectiveAngle   = cam['angle']
            c.viewExtents        = cam['extents']
            c.isSmoothTransition = False
            vp.camera = c
            vp.refresh()
        except Exception as e:
            failed.append('camera: %s' % e)

    return failed


# ── general refresh ───────────────────────────────────────────────────────────
#
# Each step is independent and failure-tolerant: a step that throws is logged and
# skipped, and nothing here may stop the flush. Only runs with no command open
# (SelectCommand, or '' — see _refresh_no_command); inside a command, clearing the
# selection would pull inputs out from under it.

RECENT_FEATURES = 20   # _refresh_feature_highlight also re-selects this many latest

_EXTRUDE_GHOST_ID = 'SmartExtrudeGhost'   # Extrude.py _GHOST_ID


def _refresh_feature_highlight(design):
    """Un-stick a FEATURE highlight Fusion keeps drawing after the selection is gone.

    Seen 2026-09-27: the 13 bodies of the newest extrude drew blue-selected with
    activeSelections empty. Clicking empty canvas, Deselect, Commands.ClearSelections,
    a viewport refresh, and selecting+clearing the bodies or the occurrence all left
    it; selecting the FEATURE (in its occurrence's context) and clearing redrew it
    clean. Which feature is stuck isn't knowable, so this cycles the likely ones —
    the active component's features and the latest RECENT_FEATURES in the timeline
    (sketches too: a hidden sketch still draws a selection highlight) — ~200 ms on a
    316-item timeline. Every feature everywhere was ~4.6 s."""
    tl = design.timeline
    root = design.rootComponent
    act = design.activeOccurrence
    act_comp = act.component if act else root
    end = min(tl.markerPosition, tl.count)
    occs = {}
    ents = []
    for i in range(end):
        try:
            item = tl.item(i)
            if item.isSuppressed or item.isGroup:
                continue
            e = item.entity
            if e is None or not hasattr(e, 'createForAssemblyContext'):
                continue
            if not (hasattr(e, 'bodies') or e.objectType == 'adsk::fusion::Sketch'):
                continue
            comp = e.parentComponent
            if comp != act_comp and i < end - RECENT_FEATURES:
                continue
            if comp == root:
                ents.append(e)
            elif comp == act_comp:
                ents.append(e.createForAssemblyContext(act))
            else:
                if comp.id not in occs:
                    occs[comp.id] = list(root.allOccurrencesByComponent(comp))
                for o in occs[comp.id]:
                    ents.append(e.createForAssemblyContext(o))
        except Exception:
            continue
    sel = ui.activeSelections
    for e in ents:
        try:
            sel.add(e)
        except Exception:
            pass
    sel.clear()
    return '%d features cycled' % len(ents)


def _stale_mesh_bodies(design):
    """Visible bodies with faces Fusion has no display mesh for (displayMeshes
    throws "aSimpleMesh"). ~5 ms for 13 bodies."""
    out = []
    for comp in design.allComponents:
        for b in comp.bRepBodies:
            if not b.isVisible:
                continue
            for f in b.faces:
                try:
                    f.meshManager.displayMeshes.bestMesh.triangleCount
                except Exception:
                    out.append(b)
                    break
    return out


def _refresh_recompute(design):
    """Faces a feature changed that never get drawn — the body is see-through there.

    Seen 2026-09-27: after the notch cut `Sketch19 (1)`, 5 of 10 faces on each of 13
    bodies had no display mesh, while the B-rep was valid and a MeshCalculator
    tessellated them fine; rolling the marker back over the cut and forward again
    redraws them. So: roll back over the last feature, or further to the latest
    feature that touches any body with unmeshed faces, then restore the marker.
    Not design.computeAll() — that crashed Fusion on this document."""
    if fvs.is_editing_sketch(design):
        return 'in a sketch, skipped'
    tl = design.timeline
    marker = tl.markerPosition
    if marker == 0:
        return 'marker at start'
    target = marker - 1
    stale = _stale_mesh_bodies(design)
    for b in stale:
        for i in range(marker - 1, -1, -1):
            try:
                e = tl.item(i).entity
                if e is not None and hasattr(e, 'bodies') and any(x == b for x in e.bodies):
                    target = min(target, i)
                    break
            except Exception:
                continue
    t = time.perf_counter()
    try:
        tl.markerPosition = target
    finally:
        tl.markerPosition = marker
    # No re-check here: every recomputed body reads as unmeshed until the next
    # redraw, which is when Fusion builds display meshes.
    return 'rolled over %d item(s) in %.0f ms, %d bodies had unmeshed faces' % (
        marker - target, (time.perf_counter() - t) * 1000, len(stale))


def _refresh_selection(design):
    """Both selection clears: the API's and Fusion's own text command."""
    ui.activeSelections.clear()
    app.executeTextCommand('Commands.ClearSelections')
    return 'cleared'


def _refresh_extrude_ghost(design):
    """A Smart Extrude ghost (red, see-through) left behind with no extrude running —
    only if the Extrude add-in has no live command, so a real preview is never
    touched."""
    for m in list(sys.modules.values()):
        if getattr(m, '_GHOST_ID', None) == _EXTRUDE_GHOST_ID and getattr(m, '_cmd', None) is not None:
            return 'extrude running, left alone'
    gone = 0
    for comp in design.allComponents:
        groups = comp.customGraphicsGroups
        for i in range(groups.count - 1, -1, -1):
            g = groups.item(i)
            if g.id == _EXTRUDE_GHOST_ID:
                g.deleteMe()
                gone += 1
    return '%d removed' % gone


def _refresh_no_command(design):
    """activeCommand '' (no SelectCommand) after a hidden-palette doExecute(True):
    selection writes from commands throw until something restarts it."""
    if ui.activeCommand == '':
        ui.commandDefinitions.itemById('SelectCommand').execute()
        return 'SelectCommand restarted'
    return 'ok'


_REFRESH_STEPS = (
    ('recompute',         _refresh_recompute),
    ('feature highlight', _refresh_feature_highlight),
    ('selection',         _refresh_selection),
    ('extrude ghost',     _refresh_extrude_ghost),
    ('no command',        _refresh_no_command),
)


def _refresh(design):
    if ui.activeCommand not in ('SelectCommand', ''):
        _log('refresh skipped: %s is running' % ui.activeCommand)
        return
    done = []
    for name, step in _REFRESH_STEPS:
        try:
            done.append('%s: %s' % (name, step(design)))
        except Exception as e:
            done.append('%s: FAILED %s' % (name, e))
    try:
        app.activeViewport.refresh()
    except Exception:
        pass
    _log('refresh ' + '; '.join(done))


# ── the flush itself ──────────────────────────────────────────────────────────

def _browser_server():
    """The Browser add-in's loaded server module, or None.

    Found by duck-typing rather than by module name so it keeps working if the
    Browser add-in is renamed or reloaded. Purely cosmetic — every caller must
    tolerate None, and a missing Browser must never affect the flush itself.
    """
    try:
        for m in list(sys.modules.values()):
            sm = getattr(m, '_server_mod', None)
            if sm is not None and hasattr(sm, 'set_freeze'):
                return sm
    except Exception:
        pass
    return None


def _freeze_browser(seconds):
    """Hold the Browser/Dock tree still across the close+reopen so it doesn't
    blank and repopulate. Time-bounded on the server side, so even if this
    add-in dies mid-flush the window recovers on its own."""
    sm = _browser_server()
    if sm is None:
        return False
    try:
        sm.set_freeze(seconds)
        return True
    except Exception:
        return False


def _resolve_doc(urn):
    """Find the open Document for a URN.  doc.save() can take seconds and pumps
    the event loop while it runs, so a Document reference captured before the
    save may be dead by the time we want to close it — always re-resolve rather
    than holding on to one across a save."""
    for d in app.documents:
        try:
            if d.dataFile and d.dataFile.id == urn:
                return d
        except Exception:
            pass
    return None


def _sketch_reentry_target(design):
    """(occ_path, sketch_name) for the sketch currently being edited, or (None,
    None) when no sketch is open or the open one could not be identified."""
    try:
        editing, sk_native, _ = fvs._edit_sketch_info(design)
    except Exception:
        return None, None
    if not editing or sk_native is None:
        return None, None
    try:
        name = sk_native.name
    except Exception:
        return None, None
    try:
        occ = fvs._active_occ(design)
        occ_path = occ.fullPathName if occ else None
    except Exception:
        occ_path = None
    return occ_path, name


def _find_sketch(design, occ_path, name):
    """Resolve a sketch by (occurrence path, name) rather than by token — the
    document has just been closed and reopened, so any token from before is
    dead."""
    if not name:
        return None
    try:
        if occ_path:
            for occ in design.rootComponent.allOccurrences:
                if occ.fullPathName == occ_path:
                    for sk in occ.component.sketches:
                        if sk.name == name:
                            if hasattr(sk, 'createForAssemblyContext'):
                                try:
                                    return sk.createForAssemblyContext(occ)
                                except Exception:
                                    return sk
                            return sk
                    return None
            return None
        for sk in design.rootComponent.sketches:
            if sk.name == name:
                return sk
    except Exception:
        pass
    return None


def _reenter_sketch(design, occ_path, name):
    """Open a sketch for edit the same way Dimension Value's _enter_sketch does:
    select it (in its occurrence's context, or it cannot be selected at all)
    and run SketchActivate. Returns True if the sketch was found and queued."""
    sk = _find_sketch(design, occ_path, name)
    if sk is None:
        return False
    try:
        sel = ui.activeSelections
        sel.clear()
        sel.add(sk)
        cd = ui.commandDefinitions.itemById('SketchActivate')
        if cd:
            cd.execute()
        return True
    except Exception:
        return False


class _NativeTrim:
    """Fusion's own undo trim, called directly — the same object chain the
    `Transaction.Trim` text command walks (NuBase TextCmdTransactionTrim::execute:
    SessionMgr::get(-1, true) → activeAssetSession() → asset()), then
    asset->transactionMgr()->trimUndo() / trimRedo() instead of its
    stopTransacting()/startTransacting(). All five are exported, found by mangled
    name in the RUNNING Fusion's libraries, and every pointer is checked before the
    next call. If anything is missing (a Fusion update renamed it), `ok` is False
    and _trim falls back to the close/reopen, so nothing gets worse."""

    _SYMS = (
        ('NuClientBase10.dylib', 'sm_get',   '_ZN2Nu10SessionMgr3getEib'),
        ('NuClientBase10.dylib', 'session',  '_ZNK2Nu10SessionMgr18activeAssetSessionEv'),
        ('NuClientBase10.dylib', 'asset',    '_ZNK2Nu12AssetSession5assetEv'),
        ('NsDataModel10.dylib',  'tm',       '_ZNK2Ns5Asset14transactionMgrEv'),
        ('NsDataModel10.dylib',  'busy',     '_ZNK2Ns14TransactionMgr22isDoingUndoRedoOrAbortEv'),
        ('NsDataModel10.dylib',  'trim_u',   '_ZN2Ns14TransactionMgr8trimUndoEv'),
        ('NsDataModel10.dylib',  'trim_r',   '_ZN2Ns14TransactionMgr8trimRedoEv'),
    )

    def __init__(self):
        import ctypes
        self.ok, self.reason = False, ''
        try:
            libc = ctypes.CDLL(None)
            libc._dyld_image_count.restype = ctypes.c_uint32
            libc._dyld_get_image_name.restype = ctypes.c_char_p
            libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            images = {}
            for i in range(libc._dyld_image_count()):
                n = libc._dyld_get_image_name(i)
                if n:
                    images[os.path.basename(n.decode())] = n.decode()
            vp = ctypes.c_void_p
            sigs = {'sm_get': (vp, [ctypes.c_int, ctypes.c_bool]), 'session': (vp, [vp]),
                    'asset': (vp, [vp]), 'tm': (vp, [vp]), 'busy': (ctypes.c_bool, [vp]),
                    'trim_u': (None, [vp]), 'trim_r': (None, [vp])}
            libs = {}
            for lib, key, sym in self._SYMS:
                if lib not in images:
                    self.reason = '%s not loaded' % lib
                    return
                if lib not in libs:
                    libs[lib] = ctypes.CDLL(images[lib])
                f = getattr(libs[lib], sym)          # AttributeError if renamed
                f.restype, f.argtypes = sigs[key]
                setattr(self, key, f)
            self.ok = True
        except Exception as e:
            self.reason = 'bind failed: %s' % e

    def txn_mgr(self):
        sm = self.sm_get(-1, True)
        sess = self.session(sm) if sm else None
        asset = self.asset(sess) if sess else None
        return self.tm(asset) if asset else None


_native_trim = None


def _native():
    global _native_trim
    if _native_trim is None:
        _native_trim = _NativeTrim()
        _log('native trim: %s' % ('ready' if _native_trim.ok else _native_trim.reason))
    return _native_trim


def _trim():
    """Drop the active document's undo/redo chain IN PLACE — no save, no version, no
    close/reopen, and it works on a never-saved document. Measured 2026-09-26: 77
    steps → 0 in 2 ms, later writes back to fresh-document speed, Undo/Redo of later
    work unaffected. Done by TransactionMgr::trimUndo/trimRedo (_NativeTrim), NOT the
    `Transaction.Trim` text command — see the docstring for what that one broke.
    Only when nothing is open ("Session" is the document's root) — never under a
    running command, and never in a sketch edit: Undo inside a sketch after a trim is
    where Fusion started crashing (2026-09-27/28, with the text command).
    _do_flush exits the sketch first and trims after.
    Returns False to fall back to the close/reopen below."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None or fvs.is_editing_sketch(design):
            _log('trim skipped: in a sketch edit')
            return False
        if not app.executeTextCommand('Transaction.List').startswith(
                'Transaction name: Session,'):
            _log('trim skipped: a transaction is open')
            return False
        n = _native()
        if not n.ok:
            return False
        tm = n.txn_mgr()
        if not tm:
            _log('trim skipped: no transaction manager for the active document')
            return False
        if n.busy(tm):
            _log('trim skipped: undo/redo/abort in progress')
            return False
        def steps(listing):
            return listing.split('Done Child Txns: ', 1)[-1].split(',', 1)[0]
        before = app.executeTextCommand('Transaction.List /All')
        n.trim_u(tm)
        n.trim_r(tm)
        after = app.executeTextCommand('Transaction.List /All')
        ok = steps(after) == '0'
        _log('trim %s: %s -> %s undo steps' % ('ok' if ok else 'FAILED', steps(before), steps(after)))
        return ok
    except Exception:
        _log('trim error ' + traceback.format_exc())
        return False


def _do_flush():
    """Entry point: called for a fresh flush request. If a sketch is open for
    edit, finish it first (SketchStop is asynchronous — this queues it and
    returns; SketchStopTerminatedHandler below continues the flush once it has
    actually completed, so _STATE['busy'] stays set across the gap to hold off
    re-entrant requests). Otherwise runs the flush directly."""
    if _STATE['busy']:
        _log('re-entrant flush ignored')
        return
    _STATE['busy'] = True
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            ui.messageBox('Flush Undo: no active design.')
            _STATE['busy'] = False
            return

        _refresh(design)

        if TRIM_IN_PLACE and _trim():
            _STATE['busy'] = False
            return

        occ_path, sk_name = _sketch_reentry_target(design)
        if sk_name:
            finish_cmd = ui.commandDefinitions.itemById('SketchStop')
            if not finish_cmd:
                ui.messageBox('Flush Undo: could not finish the sketch.')
                _STATE['busy'] = False
                return
            _log('exiting sketch %r before flush' % sk_name)
            _STATE['pending_sketch'] = (occ_path, sk_name)
            _STATE['awaiting_sketch_stop'] = True
            finish_cmd.execute()
            return  # continues in SketchStopTerminatedHandler → _do_flush_body

        _do_flush_body(None)
    except Exception:
        _log('ERROR ' + traceback.format_exc())
        try:
            ui.messageBox('Flush Undo failed:\n' + traceback.format_exc())
        except Exception:
            pass
        _STATE['busy'] = False


def _do_flush_body(sketch_reentry):
    """Does the actual close/reopen. Runs on the main thread, AFTER the command
    that triggered it has fully terminated — closing the active document from
    inside a live command is not safe, which is exactly why _do_flush defers
    here via SketchStop's own termination when a sketch had to be exited first.

    sketch_reentry is (occ_path, name) of a sketch that was open before this
    flush began and should be reopened once the document is back, or None."""
    try:
        doc = app.activeDocument
        design = adsk.fusion.Design.cast(app.activeProduct)

        if design is None:
            ui.messageBox('Flush Undo: no active design.')
            return

        # Safety fallback: _do_flush always exits a sketch edit before calling
        # here, so this should never fire — but closing the document out from
        # under a still-open sketch edit would corrupt it, so refuse rather
        # than find out.
        try:
            if fvs.is_editing_sketch(design):
                ui.messageBox('Flush Undo: could not exit the sketch, so '
                              'nothing was flushed.')
                return
        except Exception:
            pass

        # Out of the sketch now, so the in-place trim is safe; then straight back in.
        if TRIM_IN_PLACE and _trim():
            if sketch_reentry:
                occ_path, sk_name = sketch_reentry
                if _reenter_sketch(design, occ_path, sk_name):
                    _log('reentered sketch %r' % sk_name)
                else:
                    ui.messageBox('Flush Undo: flushed, but could not reopen the '
                                  'sketch %r.' % sk_name)
            return

        try:
            df = doc.dataFile
        except Exception:
            df = None
        if df is None:
            ui.messageBox('Flush Undo: save this document once before flushing '
                          '(it has never been saved, so it cannot be reopened).')
            return

        state = _capture(doc, design)
        _log('capture urn=%s occ=%s ws=%s' %
             (state['urn'], state['occ_path'], state['workspace']))

        # Cosmetic: hold the Browser/Dock tree still for the round trip. 60 s is
        # a generous ceiling on a ~4 s flush; the finally below clears it as soon
        # as the document is back.
        _log('browser freeze: %s' % ('on' if _freeze_browser(60) else 'no browser found'))

        if doc.isModified:
            try:
                doc.save('Saved by Flush Undo')
                _log('saved')
            except Exception as e:
                ui.messageBox('Flush Undo: could not save the document, so it '
                              'was not closed.\n\n%s' % e)
                return

        # Resolve the cloud DataFile while the document is still open, so the
        # reopen can start the instant the close returns.
        try:
            state['datafile'] = app.data.findFileById(state['urn'])
        except Exception:
            state['datafile'] = None

        # The save above may have run for seconds; re-resolve before closing.
        doc = _resolve_doc(state['urn'])
        if doc is None:
            _log('document already gone after save — nothing to close')
        else:
            try:
                doc.close(False)
                _log('closed')
            except Exception as e:
                if _resolve_doc(state['urn']) is None:
                    _log('close raised but document is gone: %s' % e)
                else:
                    ui.messageBox('Flush Undo: could not close the document.\n\n%s' % e)
                    return

        failed = _restore(state)
        _log('restored failures=%s' % failed)

        if sketch_reentry:
            occ_path, sk_name = sketch_reentry
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None or not _reenter_sketch(design, occ_path, sk_name):
                failed.append('could not reopen the sketch %r' % sk_name)
            else:
                _log('reentered sketch %r' % sk_name)

        # Silent on success, exactly like Reload Recent AddIns.
        if failed:
            ui.messageBox('Flush Undo finished, but some state was not restored:\n\n'
                          + '\n'.join('• ' + f for f in failed))
    except Exception:
        _log('ERROR ' + traceback.format_exc())
        try:
            ui.messageBox('Flush Undo failed:\n' + traceback.format_exc())
        except Exception:
            pass
    finally:
        # Always thaw, on every path out of the flush — including the early
        # returns for a sketch edit, an unsaved document or a failed save.
        _freeze_browser(0)
        _STATE['busy'] = False


def _schedule_flush():
    """The timer thread only calls fireCustomEvent (thread-safe by design); every
    Fusion API call stays on the main thread in _do_flush.  The timer is held in
    the shared state so a second copy of this module replaces the pending flush
    instead of queueing another one alongside it."""
    try:
        if _STATE['timer']:
            _STATE['timer'].cancel()
    except Exception:
        pass
    t = threading.Timer(0.05, lambda: app.fireCustomEvent(FLUSH_EVENT_ID, ''))
    t.daemon = True
    _STATE['timer'] = t
    t.start()


class _Flush(adsk.core.CustomEventHandler):
    def notify(self, args):
        if args.additionalInfo == 'idle':
            _idle_trim()
        else:
            _do_flush()


# ── automatic trim when idle ──────────────────────────────────────────────────

def _idle_seconds():
    """Seconds since the last real keyboard/mouse/trackpad input (HID state, so
    synthetic events posted by our own tools don't count). Thread-safe; None if
    CoreGraphics can't be reached."""
    try:
        import ctypes
        f = _STATE.get('cg_idle')
        if f is None:
            cg = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            f = cg.CGEventSourceSecondsSinceLastEventType
            f.restype = ctypes.c_double
            f.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            _STATE['cg_idle'] = f
        return f(1, 0xFFFFFFFF)   # kCGEventSourceStateHIDSystemState, any input type
    except Exception:
        return None


def _idle_trim():
    """Main thread. Trim the active document if you're still away, Fusion is idle,
    and there's anything to drop."""
    try:
        idle = _idle_seconds()
        if AUTO_TRIM_IDLE is None or idle is None or idle < AUTO_TRIM_IDLE:
            return
        if _STATE['busy'] or ui.activeCommand != 'SelectCommand':
            return
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None or fvs.is_editing_sketch(design):
            return      # a sketch edit is SelectCommand too; wait until it's closed
        listing = app.executeTextCommand('Transaction.List /All')
        def count(label):
            try:
                return int(listing.split(label + ': ', 1)[1].split(',', 1)[0].split()[0])
            except Exception:
                return 0
        if (count('Done Child Txns') == 0 and count('Undone Child Txns') == 0
                and count('Done Bulletins') < 5000):
            return      # no undo steps, only a few stray records: nothing worth it
        _STATE['busy'] = True
        try:
            _log('idle %.0f s: auto trim' % idle)
            _trim()
        finally:
            _STATE['busy'] = False
    except Exception:
        _log('idle trim error ' + traceback.format_exc())


def _idle_watch(stop_evt):
    """Background thread: nothing here touches Fusion except fireCustomEvent."""
    while not stop_evt.wait(IDLE_POLL):
        idle = _idle_seconds()
        if AUTO_TRIM_IDLE is not None and idle is not None and idle >= AUTO_TRIM_IDLE:
            try:
                app.fireCustomEvent(FLUSH_EVENT_ID, 'idle')
            except Exception:
                pass


class SketchStopTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """Continues a flush that had to exit a sketch first. Both copies of this
    module (see the process-wide _STATE note up top) register one of these on
    the same ui.commandTerminated event, so awaiting_sketch_stop — cleared as
    the first action here — is what stops the second copy from also calling
    _do_flush_body for the same flush."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if args.commandId != 'SketchStop':
                return
            if not _STATE.get('awaiting_sketch_stop'):
                return
            _STATE['awaiting_sketch_stop'] = False
            sketch_reentry = _STATE.pop('pending_sketch', None)
            _log('SketchStop terminated, continuing flush reentry=%s' % (sketch_reentry,))
            _do_flush_body(sketch_reentry)
        except Exception:
            _log('ERROR in SketchStopTerminatedHandler: ' + traceback.format_exc())
            try:
                ui.messageBox('Flush Undo failed:\n' + traceback.format_exc())
            except Exception:
                pass
            _STATE['busy'] = False


# ── command plumbing ──────────────────────────────────────────────────────────

class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _log('execute handler fired')
        try:
            _schedule_flush()
        except Exception:
            if ui:
                ui.messageBox('Error in execute:\n{}'.format(traceback.format_exc()))


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        # Stale handlers left over from a previous run() must not fire.
        if self not in handlers:
            _log('commandCreated: stale handler ignored')
            return
        _log('commandCreated fired')
        try:
            onExecute = ExecuteHandler()
            args.command.execute.add(onExecute)
            handlers.append(onExecute)
        except Exception:
            if ui:
                ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


# ── lifecycle ─────────────────────────────────────────────────────────────────

def run(context):
    global _flush_event
    try:
        # Retire any earlier copy of this module living in the same process
        # (bootstrapped instance vs. the one Fusion loads from Scripts and
        # Add-Ins).  Without this both copies keep a handler on the custom event
        # and one hotkey press starts two concurrent flushes.
        prev_stop = _STATE.get('stop')
        if prev_stop is not None and prev_stop is not stop:
            try:
                prev_stop(None)
                _log('retired a previous instance of the add-in')
            except Exception:
                pass
        _STATE['stop'] = stop

        cmdDefs = ui.commandDefinitions
        cmdDef = cmdDefs.itemById(CMD_ID)
        if not cmdDef:
            cmdDef = cmdDefs.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        onCommandCreated = CommandCreatedHandler()
        cmdDef.commandCreated.add(onCommandCreated)
        handlers.append(onCommandCreated)

        try:
            _flush_event = app.registerCustomEvent(FLUSH_EVENT_ID)
        except Exception:
            _flush_event = app.customEvents.itemById(FLUSH_EVENT_ID)
        fh = _Flush()
        _flush_event.add(fh)
        handlers.append(fh)

        onSketchStop = SketchStopTerminatedHandler()
        ui.commandTerminated.add(onSketchStop)
        handlers.append(onSketchStop)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmdDef)

        idle_stop = threading.Event()
        _STATE['idle_stop'] = idle_stop
        threading.Thread(target=_idle_watch, args=(idle_stop,), daemon=True).start()

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        try:
            if _STATE.get('timer'):
                _STATE['timer'].cancel()
                _STATE['timer'] = None
        except Exception:
            pass
        try:
            if _STATE.get('idle_stop'):
                _STATE['idle_stop'].set()
                _STATE['idle_stop'] = None
        except Exception:
            pass
        _STATE['busy'] = False
        _STATE['awaiting_sketch_stop'] = False
        _STATE['pending_sketch'] = None
        if _STATE.get('stop') is stop:
            _STATE['stop'] = None

        for h in handlers:
            if _flush_event:
                try:
                    _flush_event.remove(h)
                except Exception:
                    pass
            try:
                ui.commandTerminated.remove(h)
            except Exception:
                pass
        try:
            app.unregisterCustomEvent(FLUSH_EVENT_ID)
        except Exception:
            pass

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()

        # Do NOT call cmdDef.deleteMe() — that destroys the shortcut binding.
        handlers.clear()
    except Exception:
        pass
