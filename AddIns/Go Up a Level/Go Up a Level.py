import adsk.core, adsk.fusion, traceback, os, sys, datetime, threading

import types as _types
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load('fvs', 'Visibility Snapshot.py')

_LOG = '/tmp/fusion_sketch_entry_debug.log'
def _log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S.%f')
    with open(_LOG, 'a') as f:
        f.write(f'[{ts}] [GoUp] {msg}\n')

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

CMD_ID = 'GoToParentComponentCmd'
CMD_NAME = 'Go Up a Level'
CMD_DESC = 'Activate and isolate the parent of the active component'

SELECTION_EFFECT_NORMAL   = 'Normal Selection Effect'
_saved_selection_effect   = None
_came_from_sketch         = False
_pending_sketch_parent_occ = None
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


class _DocSnapshotHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                fvs.take_snapshot(design, None)
        except Exception:
            pass

SKETCH_EXIT_EVENT = 'GoUpALevel_SketchExit'
_sketch_exit_event = None
_sketch_exit_handler = None
_pending_exit_occ = None


# ─────────────────────────────────────────────
# Display Style
# ─────────────────────────────────────────────
def _get_selection_effect_lcd():
    cd = ui.commandDefinitions.itemById('ViewSelectionEffectCommand')
    if cd:
        return adsk.core.ListControlDefinition.cast(cd.controlDefinition)
    return None


# Selection-effect switching has been removed — the viewport now always stays
# on whatever effect the user has set (Simple).  These are now no-ops so the
# existing call sites need no edits.  The old behaviour flipped to 'Normal
# Selection Effect' during the activation and restored the previously-selected
# listItem afterwards:
#
#   lcd = ViewSelectionEffectCommand's ListControlDefinition
#   # set:     item('Normal Selection Effect').isSelected = True
#   # restore: item(saved_name).isSelected = True
#
# Reinstate from git history / the block above if ever needed again.
def display_style_set_normal():
    pass


def display_style_restore():
    pass


# ─────────────────────────────────────────────
# Deferred sketch-exit isolation (custom event)
# ─────────────────────────────────────────────
class SketchExitEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _pending_exit_occ
        try:
            occ = _pending_exit_occ
            _pending_exit_occ = None
            _log(f'SketchExit deferred event: occ={occ.name if occ else None}')
            if not occ:
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            try:
                parent_occ = occ.assemblyContext
            except Exception:
                parent_occ = None
            _log(f'  parent_occ: {parent_occ.name if parent_occ else None}')

            with fvs.one_step('Navigate'):   # the whole jump is one undo step
                if fvs.isolate_is_active():
                    fvs.isolate_occurrence_no_snapshot(design, occ)
                fvs.restore_snapshot(design, occ)
                occ.activate()
                fvs.remember_activation(occ)
                fvs.apply_policy(design)
            ui.activeSelections.clear()

            _log(f'  done, activeComp={design.activeComponent.name if design.activeComponent else "none"}')
        except:
            _log(f'SketchExit deferred error: {traceback.format_exc()}')


# ─────────────────────────────────────────────
# Run / Stop
# ─────────────────────────────────────────────
def run(context):
    global _sketch_exit_event, _sketch_exit_handler, _doc_snapshot_handler

    try:
        cmdDefs = ui.commandDefinitions
        cmdDef = cmdDefs.itemById(CMD_ID)
        if not cmdDef:
            cmdDef = cmdDefs.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        onCommandCreated = CommandCreatedHandler()
        cmdDef.commandCreated.add(onCommandCreated)
        handlers.append(onCommandCreated)

        onCommandStarting = CommandStartingHandler()
        ui.commandStarting.add(onCommandStarting)
        handlers.append(onCommandStarting)

        onCommandTerminated = CommandTerminatedHandler()
        ui.commandTerminated.add(onCommandTerminated)
        handlers.append(onCommandTerminated)

        _sketch_exit_event = app.registerCustomEvent(SKETCH_EXIT_EVENT)
        _sketch_exit_handler = SketchExitEventHandler()
        _sketch_exit_event.add(_sketch_exit_handler)

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
    global _doc_snapshot_handler
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
        app.unregisterCustomEvent(SKETCH_EXIT_EVENT)
        if _doc_snapshot_handler:
            app.documentOpened.remove(_doc_snapshot_handler)
            _doc_snapshot_handler = None
        handlers.clear()
    except:
        pass


# ─────────────────────────────────────────────
# Command Starting — exit sketch before we run
# ─────────────────────────────────────────────
class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _came_from_sketch, _pending_sketch_parent_occ
        try:
            if self not in handlers:
                return
            if args.commandId != CMD_ID:
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            # Suspend mode (fvs § Suspend mode: peek): up is a peek at the view's
            # parent, run from the veto, so the command never starts and the running
            # command, the sketch and the active component all stay put.
            if fvs.is_suspended():
                args.isCanceled = True
                _came_from_sketch = False
                _pending_sketch_parent_occ = None
                fvs.peek_up(design)
                return
            # Detect the sketch edit via SketchStop.isEnabled — design.activeEditObject
            # raises InternalValidationError for a sketch nested in an occurrence, which
            # would make us silently fall through to the normal "go up a component" path
            # (exiting the sketch AND jumping up a level). See active_edit_object_occ_raises.
            if fvs.is_editing_sketch(design) and fvs.request_master_return(design):
                # A master opened by Edit Original: going up means going BACK to
                # the sketch it was opened from, which MasterReturnCmd now does.
                _came_from_sketch = False
                _pending_sketch_parent_occ = None
                args.isCanceled = True
                return
            if fvs.is_editing_sketch(design):
                # Identity comes from fvs (activeEditObject raises at depth), with the
                # timeline marker as the last resort when nothing recorded the entry.
                sketch = (fvs.active_edit_sketch(design)
                          or fvs.edited_sketch_from_marker(design))

                _came_from_sketch = True
                _log(f'in sketch: {sketch.name if sketch else "?"!r}, '
                     f'parentComp={sketch.parentComponent.name if sketch else "?"!r}')
                saved = fvs.recall_sketch_entry_occ(design)
                _log(f'  recall_sketch_entry_occ: {saved.name if saved else None}')
                fvs.clear_sketch_entry_occ()
                fallback = fvs.find_occurrence(design, sketch.parentComponent) if sketch else None
                _log(f'  find_occurrence fallback: {fallback.name if fallback else None}')
                # Land in the sketch's parent component (the occurrence active at sketch
                # entry), NOT one level higher.
                _pending_sketch_parent_occ = saved or fallback
                _log(f'  -> _pending_sketch_parent_occ: {_pending_sketch_parent_occ.name if _pending_sketch_parent_occ else None}')
                finish_cmd = ui.commandDefinitions.itemById('SketchStop')
                if finish_cmd:
                    finish_cmd.execute()
            else:
                _came_from_sketch = False
                _pending_sketch_parent_occ = None
        except:
            _log(f'CommandStartingHandler error: {traceback.format_exc()}')


# ─────────────────────────────────────────────
# Command Terminated
# ─────────────────────────────────────────────
class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _came_from_sketch, _pending_sketch_parent_occ, _pending_exit_occ
        try:
            if self not in handlers:
                return
            if args.commandId != 'SketchStop' or not _came_from_sketch:
                return
            _came_from_sketch = False
            occ = _pending_sketch_parent_occ
            _pending_sketch_parent_occ = None
            _log(f'SketchStop terminated: occ={occ.name if occ else None}')

            # The timeline marker's exit safety net lives in Go Down's SketchStop
            # listener, which runs before this handler's deferred work.

            # Apply -delta HERE — this handler fires synchronously inside
            # SketchStop.execute(), so the correction lands before any render.
            delta = fvs.recall_sketch_cam_delta()
            _log(f'  delta={delta}')
            if delta:
                try:
                    dx, dy, dz = delta
                    vp  = app.activeViewport
                    cam = vp.camera
                    e, t = cam.eye, cam.target
                    _log(f'  cam before: eye=({e.x:.1f},{e.y:.1f},{e.z:.1f})')
                    cam.eye    = adsk.core.Point3D.create(e.x - dx, e.y - dy, e.z - dz)
                    cam.target = adsk.core.Point3D.create(t.x - dx, t.y - dy, t.z - dz)
                    cam.isSmoothTransition = False
                    vp.camera = cam
                    _log(f'  cam after:  eye=({cam.eye.x:.1f},{cam.eye.y:.1f},{cam.eye.z:.1f})')
                except Exception:
                    _log(f'  cam correction error: {traceback.format_exc()}')
                fvs.clear_sketch_cam()

            _pending_exit_occ = occ
            def _fire():
                try:
                    app.fireCustomEvent(SKETCH_EXIT_EVENT, '')
                except Exception:
                    pass
            threading.Thread(target=_fire, daemon=True).start()
        except:
            _log(f'CommandTerminatedHandler error: {traceback.format_exc()}')


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
# Execute
# ─────────────────────────────────────────────
class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            if _came_from_sketch:
                return

            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return

            active_comp = design.activeComponent

            if active_comp and active_comp.name == 'Top Level':
                return  # already at top — keep selection, nothing above

            # Keep the current geometry selected across the activation.
            # Not a timeline feature, though: jumping out drops it, like jumping between
            # components does. A feature pick (a feature row, the Timeline's Trace) brings
            # its sketch and edges along, so the whole pick goes, not just the feature.
            kept = [ui.activeSelections.item(i).entity
                    for i in range(ui.activeSelections.count)]
            if any(isinstance(e, adsk.fusion.Feature) for e in kept):
                kept = []

            recalled = fvs.recall_active_occ(design)
            recalled_valid = recalled and recalled.component == active_comp
            active_occ = recalled if recalled_valid else fvs.find_occurrence(design, active_comp)
            fvs.take_snapshot(design, active_occ)

            parent_occ = active_occ.assemblyContext if active_occ else None
            # Climb past pass-through wrappers: the browser doesn't show them, so going
            # up must not land in one. All the way to root if that's all there is.
            while parent_occ is not None and fvs.is_disregarded(parent_occ):
                parent_occ = parent_occ.assemblyContext
            ui.activeSelections.clear()

            if parent_occ:
                fvs.restore_snapshot(design, parent_occ)
                parent_occ.activate()
                fvs.remember_activation(parent_occ)
            else:
                fvs.restore_snapshot(design, None)
                design.activateRootComponent()
                fvs.remember_activation(None)   # root is active: clear the mirror

            fvs.apply_policy(design)
            _reselect(kept)

        except:
            if ui:
                ui.messageBox('Error in execute:\n{}'.format(traceback.format_exc()))
