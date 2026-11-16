import adsk.core, adsk.fusion, ctypes, threading, traceback, os, sys, datetime

_LOG = '/tmp/fusion_sketch_entry_debug.log'
def _log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S.%f')
    with open(_LOG, 'a') as f:
        f.write(f'[{ts}] [GoTop] {msg}\n')

import types as _types
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load('fvs', 'Visibility Snapshot.py')

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

CMD_ID = 'UnisolateTopLevelCmd'
CMD_NAME = 'Go to Top Level'
CMD_DESC = 'Activate the Top Level component and restore its visibility'

# The jump does NOT run as a Fusion command.
#
# Pressed as a hotkey, UnisolateTopLevelCmd became the active command, and whatever
# Fusion then did with it — the log shows an UndoCommand landing ~80 ms after the jump
# had already finished, every time, and never once on the identical jump driven from
# the Browser add-in's poll — rolled the whole thing straight back: the viewport flicked
# to the top level and returned to where it started. The three-finger tap never showed
# it because the work is the same either way; only the command lifecycle around it
# differed. So there is no lifecycle now: commandStarting refuses the command outright
# (isCanceled, which also stops it terminating whatever command was running — see the
# Browser add-in's HotkeyStartingHandler) and the jump runs from a custom event on the
# next turn of the event loop, the way Click To Descend runs its deferred descend.
RUN_EVENT_ID = 'GoToTopLevel_Run_001'
_run_event = None

_saved_selection_effect   = None
_doc_snapshot_handler     = None


def _reselect(kept):
    """Re-apply a captured selection after an activation (which clears it)."""
    sel = ui.activeSelections
    sel.clear()
    for e in kept:
        try:
            sel.add(e)
        except Exception:
            pass


def _unisolate_path(design, active_occ):
    """Restore saved visibility at EVERY level from root down to active_occ.

    Going to the top from N levels deep must un-hide the siblings that were
    hidden (by isolate) at each intermediate level, not just at root — otherwise
    components nested between the top and where we were stay hidden.
    """
    # Bulbs come from the ledger — an exact undo of what isolation changed, with no
    # guessing about intent. Folder (body/sketch group) state is NOT in the ledger and
    # still has to come from the snapshot, level by level.
    path = []
    cur = active_occ
    while cur is not None:
        path.append(cur)
        try:
            cur = cur.assemblyContext
        except Exception:
            break
    path.reverse()

    if fvs.unisolate_from_ledger(design):
        fvs.restore_folders_only(design, None)
        for occ in path:
            fvs.restore_folders_only(design, occ)
        return

    # Fallback: nothing recorded (a session predating the ledger) — replay snapshots.
    fvs.restore_snapshot(design, None)
    if not active_occ:
        return
    for occ in path:
        fvs.restore_snapshot(design, occ)


class _DocSnapshotHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                fvs.take_snapshot(design, None)
        except Exception:
            pass


# True from when commandStarting fires until commandTerminated(SketchStop) fires.
# Must NOT be reset in CommandExecuteHandler — SketchStop is async and commandTerminated
# fires after CommandExecuteHandler.
_came_from_sketch = False

SELECTION_EFFECT_NORMAL = 'Normal Selection Effect'


# ─────────────────────────────────────────────
# Display Style
# ─────────────────────────────────────────────
def _get_selection_effect_lcd():
    cd = ui.commandDefinitions.itemById('ViewSelectionEffectCommand')
    if cd:
        return adsk.core.ListControlDefinition.cast(cd.controlDefinition)
    return None


# Selection-effect switching removed — viewport stays on the user's effect
# (Simple).  No-ops kept so existing call sites need no edits.  The old logic
# flipped to 'Normal Selection Effect' during activation and restored the
# previously-selected listItem afterwards (see git history / Go Down's comment).
def display_style_set_normal():
    pass


def display_style_restore():
    pass


# ─────────────────────────────────────────────
# Sketch state
# ─────────────────────────────────────────────
def _in_sketch():
    """True while a sketch is open for edit, AT ANY DEPTH.

    Delegates to fvs, which reads SketchStop.isEnabled. This used to test
    design.activeEditObject directly, and that property raises
    InternalValidationError for a sketch nested inside an occurrence — the normal
    case once you have descended into a sub-component. The raise was swallowed and
    read as "not in a sketch", so the jump took the non-sketch path below: it never
    queued SketchStop, and you ended up at the top level with the sketch still open
    behind an unrelated active component. Root-level sketches exited correctly,
    which is what made it look intermittent.
    """
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        return design is not None and fvs.is_editing_sketch(design)
    except Exception:
        return False


# ─────────────────────────────────────────────
# Refocus helper
# ─────────────────────────────────────────────
def _refocus():
    try:
        _objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        _objc.objc_getClass.restype    = ctypes.c_void_p
        _objc.objc_getClass.argtypes   = [ctypes.c_char_p]
        _objc.sel_registerName.restype  = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc.objc_msgSend.restype      = ctypes.c_void_p
        _objc.objc_msgSend.argtypes     = [ctypes.c_void_p, ctypes.c_void_p]
        cls     = _objc.objc_getClass(b'NSRunningApplication')
        cur_app = _objc.objc_msgSend(cls, _objc.sel_registerName(b'currentApplication'))
        f = _objc.objc_msgSend
        f.restype  = ctypes.c_bool
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        f(cur_app, _objc.sel_registerName(b'activateWithOptions:'), ctypes.c_ulong(2))
    except Exception:
        pass


class _RunHandler(adsk.core.CustomEventHandler):
    """The jump itself, one turn of the event loop after the refused command."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            with fvs.one_step('Navigate'):   # the whole jump is one undo step
                go_to_top()
        except Exception:
            _log(f'RunHandler error: {traceback.format_exc()}')


# ─────────────────────────────────────────────
# Run / Stop
# ─────────────────────────────────────────────
def run(context):
    global _doc_snapshot_handler, _run_event
    try:
        cmdDefs = ui.commandDefinitions
        cmdDef = cmdDefs.itemById(CMD_ID)
        if not cmdDef:
            cmdDef = cmdDefs.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        # Re-registering after a reload raises rather than returning the existing
        # event, so fall back to looking it up. _run_event staying None is what the
        # commandStarting veto tests before it refuses anything.
        try:
            _run_event = app.registerCustomEvent(RUN_EVENT_ID)
        except Exception:
            try:
                _run_event = app.customEvents.itemById(RUN_EVENT_ID)
            except Exception:
                _run_event = None
        if _run_event:
            onRun = _RunHandler()
            _run_event.add(onRun)
            handlers.append(onRun)

        onCommandCreated = CommandCreatedHandler()
        cmdDef.commandCreated.add(onCommandCreated)
        handlers.append(onCommandCreated)

        onCommandStarting = AppCommandStartingHandler()
        ui.commandStarting.add(onCommandStarting)
        handlers.append(onCommandStarting)

        onCommandTerminated = CommandTerminatedHandler()
        ui.commandTerminated.add(onCommandTerminated)
        handlers.append(onCommandTerminated)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmdDef)

        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                fvs.take_snapshot(design, None)
        except Exception:
            pass

        _doc_snapshot_handler = _DocSnapshotHandler()
        app.documentOpened.add(_doc_snapshot_handler)

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _doc_snapshot_handler, _run_event
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
        try:
            app.unregisterCustomEvent(RUN_EVENT_ID)
        except Exception:
            pass
        _run_event = None
        if _doc_snapshot_handler:
            app.documentOpened.remove(_doc_snapshot_handler)
            _doc_snapshot_handler = None
        handlers.clear()
    except:
        pass


# ─────────────────────────────────────────────
# Command Starting — queue SketchStop if in sketch
# ─────────────────────────────────────────────
class AppCommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _came_from_sketch
        try:
            if self not in handlers:
                return
            if args.commandId != CMD_ID:
                return
            # Suspend mode (fvs § Suspend mode: peek): the top, as a peek — refused
            # and done here, no sketch exit, no activation.
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design and fvs.is_suspended():
                args.isCanceled = True
                _came_from_sketch = False
                top_occ = next((o for o in design.rootComponent.occurrences
                                if o.component.name == 'Top Level'), None)
                fvs.peek(design, top_occ)
                return
            if not _in_sketch():
                _came_from_sketch = False
                # Queue the jump BEFORE refusing the command: the veto is only safe
                # once there is something to run in its place, so if the event can't
                # be fired the command is left to run the old way rather than
                # cancelled into doing nothing at all.
                try:
                    if not _run_event:
                        raise RuntimeError('no run event')
                    app.fireCustomEvent(RUN_EVENT_ID, '')
                except Exception:
                    _log('AppCommandStarting: fireCustomEvent failed — command left to run')
                    return
                args.isCanceled = True
                _log('AppCommandStarting: vetoed, jump queued')
                return

            # A master opened by Edit Original: leaving it goes BACK to the sketch it
            # was opened from (MasterReturnCmd), not to the top.
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design and fvs.request_master_return(design):
                _came_from_sketch = False
                args.isCanceled = True
                _log('AppCommandStarting: master session — returning to original sketch')
                return

            _came_from_sketch = True
            # Refused here too — SketchStop, queued below, carries the jump out from
            # its own terminated handler. See RUN_EVENT_ID.
            args.isCanceled = True

            # Queue SketchStop. It runs asynchronously — after this handler returns
            # and after CommandExecuteHandler runs. Camera correction and visibility
            # restoration are both handled in CommandTerminatedHandler once SketchStop
            # actually completes.
            finish_cmd = ui.commandDefinitions.itemById('SketchStop')
            if finish_cmd:
                finish_cmd.execute()

            _log('AppCommandStarting: SketchStop queued')

        except:
            _log(f'AppCommandStarting error: {traceback.format_exc()}')


# ─────────────────────────────────────────────
# Command Terminated — SketchStop fires here AFTER it actually runs
# ─────────────────────────────────────────────
class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _came_from_sketch
        try:
            if self not in handlers:
                return
            if args.commandId == CMD_ID:
                try:
                    _t('terminated reason=%s' % args.terminationReason)
                except Exception:
                    _t('terminated reason=?')
            if args.commandId != 'SketchStop' or not _came_from_sketch:
                return

            # Reset flag here — SketchStop has fully completed by this point.
            _came_from_sketch = False

            # ── Camera correction ──────────────────────────────────────────
            delta = fvs.recall_sketch_cam_delta()
            _log(f'SketchStop terminated: delta={delta}')
            if delta:
                try:
                    dx, dy, dz = delta
                    vp  = app.activeViewport
                    cam = vp.camera
                    e, t = cam.eye, cam.target
                    cam.eye    = adsk.core.Point3D.create(e.x - dx, e.y - dy, e.z - dz)
                    cam.target = adsk.core.Point3D.create(t.x - dx, t.y - dy, t.z - dz)
                    cam.isSmoothTransition = False
                    vp.camera = cam
                    _log(f'  cam corrected: eye=({cam.eye.x:.1f},{cam.eye.y:.1f},{cam.eye.z:.1f})')
                except Exception:
                    _log(f'  cam error: {traceback.format_exc()}')
                fvs.clear_sketch_cam()

            # ── Visibility restoration (same handler = same render frame as camera) ──
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            root = design.rootComponent

            # Roll the timeline marker back to its pre-sketch position before any
            # snapshot/visibility work — Fusion's exit-restore can land the marker
            # short when it was moved during the edit, leaving later structural
            # ops (e.g. CutPaste component moves) reverted.
            with fvs.one_step('Navigate'):   # the whole jump is one undo step
                try:
                    fvs.restore_pre_sketch_marker(design)
                except Exception:
                    pass

                display_style_set_normal()

                active_comp = design.activeComponent
                recalled = fvs.recall_active_occ(design)
                active_occ = (recalled if (recalled and recalled.component == active_comp)
                              else fvs.find_occurrence(design, active_comp))
                fvs.take_snapshot(design, active_occ)

                top_occ = next(
                    (o for o in root.occurrences if o.component.name == 'Top Level'),
                    None,
                )

                _unisolate_path(design, active_occ)

                if top_occ:
                    top_occ.activate()
                    fvs.remember_activation(top_occ)
                    fvs.restore_snapshot(design, top_occ)
                else:
                    design.activateRootComponent()
                    fvs.remember_activation(None)   # root is active: clear the mirror

                fvs.apply_policy(design)
            ui.activeSelections.clear()

            _log('CommandTerminated: restoration done')
            threading.Timer(0.8, _refocus).start()

        except:
            _log(f'CommandTerminated error: {traceback.format_exc()}')


# ─────────────────────────────────────────────
# Command Created
# ─────────────────────────────────────────────
class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            onExecute = CommandExecuteHandler()
            args.command.execute.add(onExecute)
            handlers.append(onExecute)
        except:
            if ui:
                ui.messageBox('Error in commandCreated:\n{}'.format(traceback.format_exc()))


# ─────────────────────────────────────────────
# Execute — non-sketch case only
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# The jump itself, as a plain function.
#
# It used to live only inside CommandExecuteHandler.notify, so the ONLY way to run it
# was through Fusion's command lifecycle. cmd_def.execute() merely QUEUES a command —
# it runs when the event loop next turns — so a script driving it had to pump the loop
# and hope, and pumping the loop from outside is itself unsafe. The practical effect was
# that this could not be exercised from a test at all, and a test which cannot run the
# thing it measures silently measures nothing. Extracted so the browser, other add-ins
# and the harness can call it directly and synchronously; the command handler below is
# now a thin wrapper around it.
# ─────────────────────────────────────────────
def _t(msg):
    try:
        import time as _time
        with open('/tmp/click_to_descend.log', 'a') as f:
            f.write('%.3f  [gotop] %s\n' % (_time.time(), msg))
    except Exception:
        pass


def go_to_top(design=None):
    """Jump to the top level and restore visibility. Safe to call directly."""
    design = design or adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return False
    root = design.rootComponent

    # Keep the current geometry selected across the activation.
    kept = [ui.activeSelections.item(i).entity
            for i in range(ui.activeSelections.count)]

    _t('start active=%s' % design.activeComponent.name)
    active_comp = design.activeComponent
    recalled = fvs.recall_active_occ(design)
    active_occ = (recalled if (recalled and recalled.component == active_comp)
                  else fvs.find_occurrence(design, active_comp))
    fvs.take_snapshot(design, active_occ)
    _t('snapshot done')

    top_occ = next(
        (o for o in root.occurrences if o.component.name == 'Top Level'),
        None,
    )

    _unisolate_path(design, active_occ)
    _t('unisolate done')

    if top_occ:
        top_occ.activate()
        fvs.remember_activation(top_occ)
        fvs.restore_snapshot(design, top_occ)
    else:
        design.activateRootComponent()
        fvs.remember_activation(None)   # root is active: clear the mirror
    _t('activate done')

    fvs.apply_policy(design)
    _t('policy done')
    _reselect(kept)
    _t('reselect done active=%s' % design.activeComponent.name)

    threading.Timer(0.8, _refocus).start()

    return True


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            # Fallback only: AppCommandStartingHandler refuses the command before it
            # gets this far, so reaching here means the veto did not take.
            _log('execute handler reached — veto did not take')
            # SketchStop was queued in AppCommandStartingHandler and runs asynchronously,
            # so all sketch-exit work belongs to CommandTerminatedHandler instead.
            if _came_from_sketch:
                return
            go_to_top()
        except:
            if ui:
                ui.messageBox('Error in execute:\n{}'.format(traceback.format_exc()))
