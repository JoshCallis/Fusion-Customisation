"""
Go Down a Level  (incremental descent)
──────────────────────────────────────
Each invocation activates ONE component level toward whatever geometry is
selected, and KEEPS that geometry selected so the command can be pressed
repeatedly to walk all the way down to the parent component of the selected
sketch / body.

  • Selection stays put across the activation (capture → activate → re-add, all
    in one synchronous event batch so only a single frame renders — no flash).
  • At the parent component of a selected SKETCH, the next press enters sketch
    edit.  For a body, descent simply stops there.
  • Isolate, snapshot and the folder-visibility policy are applied at each step.

While editing a sketch, the command instead extends the selection to the
connected sketch curves (an orthogonal convenience that predates the rewrite).

SELECTION-EFFECT NOTE
─────────────────────
This command used to flip the viewport selection effect to "Normal Selection
Effect" during activation and restore it afterwards (and the sketch path saved
/ restored it around SketchActivate).  That is gone — everything now stays on
the Simple/whatever effect the user has set.  For reference, the old mechanism
was:

    cd  = ui.commandDefinitions.itemById('ViewSelectionEffectCommand')
    lcd = adsk.core.ListControlDefinition.cast(cd.controlDefinition)
    for item in lcd.listItems:
        if item.name == 'Normal Selection Effect':
            item.isSelected = True          # set
    # ...later, restore the previously-selected listItem by name.

Reinstate that pair of helpers if selection-effect switching is ever wanted
again.
"""

import adsk.core, adsk.fusion, traceback
import os
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load_module(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load_module('fvs', 'Visibility Snapshot.py')

CMD_ID    = 'ActivateAndIsolateCmd'
CMD_NAME  = 'Go Down a Level'
CMD_TIP   = 'Activate one component level toward the selected geometry'
PANEL_ID  = 'SolidScriptsAddinsPanel'

_app = None
_ui = None
_handlers = []
_sketch_activate_listener = None
_sketch_activate_starting_listener = None
_suspend_starting_listener = None
_doc_snapshot_handler = None


class _DocSnapshotHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if design:
                fvs.take_snapshot(design, None)
        except Exception:
            pass


# ----------------------------
# SKETCH LISTENERS  (timeline-roll correction; see fvs)
# ----------------------------
class SketchActivateTerminatedListener(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if args.commandId == 'SketchCreate':
                return
            if args.commandId in ('SketchActivate', 'SketchStop'):
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if design:
                    with fvs.one_step('Visibility'):   # all of it is one undo step
                        self._settle(design, args)
        except Exception:
            pass

    def _settle(self, design, args):
                    _sk = None
                    if args.commandId == 'SketchActivate':
                        # Identify the edited sketch from the timeline marker, which
                        # Fusion has rolled to just after it.  This is the reliable,
                        # entry-method-independent identity source (double-click
                        # entry clears the selection before the starting-listener
                        # fires, so that capture misses it).
                        try:
                            _sk = fvs.edited_sketch_from_marker(design)
                            if _sk:
                                fvs.remember_edit_sketch(_sk)
                        except Exception:
                            _sk = None
                    else:
                        # The marker is left where Fusion rolls it for the edit (at
                        # the sketch) — rolling it forward on entry recomputed the
                        # whole rest of the timeline on every entry AND on every
                        # edit inside the sketch. Fusion rolls it back to the end
                        # itself on exit; this only catches it landing short, and
                        # is a no-op otherwise.
                        fvs.restore_pre_sketch_marker(design)
                    if args.commandId == 'SketchActivate':
                        # Reveal the two in-plane origin axes + show editing aids.
                        fvs.reveal_sketch_axes(design)
                        fvs.show_sketch_aids(design)
                        try:
                            fvs._log('GODOWN activate-terminated',
                                     'marker_sketch=' + (_sk.name if _sk else 'None'),
                                     'active=' + design.activeComponent.name)
                        except Exception:
                            pass
                        # Bodies default OFF while sketching (exit puts them
                        # back ON).  Must run before apply_policy so the hidden
                        # default renders.
                        fvs.enter_sketch_body_default(design)
                        # A master session (Edit Original) moves phase here — and on
                        # the way back puts the pre-hop view state over the entry
                        # defaults just written. Before apply_policy, which reads both.
                        fvs.master_session_on_activate(
                            design, _sk or fvs.recall_edit_sketch(design))
                        # Tell apply_policy explicitly which sketch is being
                        # edited (force_edit_sketch) — don't make it re-detect,
                        # since SketchStop.isEnabled / activeEditObject can be
                        # momentarily unsettled right after enter.  Only the edited
                        # sketch shows by default; the toggle gates the others.
                        fvs.apply_policy(design, force_edit_sketch=_sk)
                    elif fvs.master_hop_in_progress():
                        # Edit Original's hop between the original sketch and its
                        # master: not a real exit. Only tidy the sketch being left
                        # (axes, annotations, edit identity) — the restores below
                        # would switch the whole model's sketches and bodies back on
                        # for the moment before the next sketch opens.
                        fvs.restore_origin_default(design)
                        fvs.hide_sketch_aids(design)
                    else:  # SketchStop → hide origin geometry + construction aids.
                        # SketchStop is still enabled for a beat after it ran —
                        # say "closed" before anything below reads it.
                        fvs.note_sketch_exit()
                        # Restore per-sketch bulbs first so any sketches hidden
                        # during the edit aren't stranded-off once the folder
                        # becomes the sole authority again.
                        fvs.restore_sketch_bulbs(design)
                        fvs.restore_origin_default(design)
                        fvs.hide_sketch_aids(design)
                        # Bodies back to the component default (ON), whatever
                        # the toggle was doing during the edit.  Before
                        # set_sketches_shown so the restored value is in place
                        # for that apply_policy.
                        fvs.exit_sketch_restore_body(design)
                        # The other sketches take the arrival default on exit —
                        # off (bodies only) if the component has bodies, else on —
                        # pinned until you leave the component.  Re-applies the
                        # policy itself.
                        fvs.exit_sketch_restore_sketches(design)


class SketchActivateStartingListener(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            if args.commandId != 'SketchActivate':
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                return
            # Not activeEditObject: it raises for a sketch nested in an occurrence,
            # and the old `except: already_editing = False` then re-remembered the
            # pre-sketch marker on top of the real one.
            if fvs.is_editing_sketch(design):
                return
            fvs.remember_pre_sketch_marker(design)
            # Capture the sketch about to be edited straight from the selection.
            # This is the reliable identity source for BOTH manual double-click
            # entry and Go Down's programmatic entry — design.activeEditObject is
            # unusable for a sketch nested in an occurrence (it raises).  Clear
            # first so a failed capture can't leave a stale sketch identified.
            try:
                fvs.clear_edit_sketch()
                sel = _ui.activeSelections
                for i in range(sel.count):
                    sk = get_sketch(sel.item(i).entity)
                    if sk:
                        fvs.remember_edit_sketch(sk)
                        break
            except Exception:
                pass
        except Exception:
            pass


class SuspendStartingListener(adsk.core.ApplicationCommandEventHandler):
    """Suspend mode (fvs § Suspend mode: peek): one level toward the selection, as a
    peek, run from the veto — the command never starts, so the running command, the
    sketch, the selection and the active component all stay put. Nothing at the
    bottom: entering a sketch or promoting a pick would disturb exactly those."""
    def notify(self, args):
        try:
            if self not in _handlers or args.commandId != CMD_ID:
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design or not fvs.is_suspended():
                return
            args.isCanceled = True
            sel = _ui.activeSelections
            if sel.count:
                fvs.peek_toward(design, get_occurrence(sel.item(0).entity))
        except Exception:
            pass


# ----------------------------
# ENTITY HELPERS
# ----------------------------
def get_sketch(entity):
    """Return the Sketch if entity is a sketch or sketch element, else None."""
    if isinstance(entity, adsk.fusion.Sketch):
        return entity
    if hasattr(entity, 'parentSketch') and entity.parentSketch:
        return entity.parentSketch
    return None


def get_occurrence(entity):
    """Return the occurrence directly owning the geometry (its parent-component
    instance), or None when the geometry lives at root level."""
    if isinstance(entity, adsk.fusion.Occurrence):
        return entity
    if hasattr(entity, 'assemblyContext') and entity.assemblyContext:
        return entity.assemblyContext
    if hasattr(entity, 'body') and entity.body and entity.body.assemblyContext:
        return entity.body.assemblyContext
    if hasattr(entity, 'parentSketch') and entity.parentSketch and entity.parentSketch.assemblyContext:
        return entity.parentSketch.assemblyContext
    return None


def _is_prefix(short, full):
    return full == short or full.startswith(short + '/')


def _reselect(kept):
    sel = _ui.activeSelections
    sel.clear()
    for e in kept:
        try:
            sel.add(e)
        except Exception:
            pass


def _promote_partial_body(entity):
    """At the bottom of descent, if `entity` is a sub-entity of a body (a selected
    face / edge / vertex), replace the selection with the WHOLE in-context body, so a
    partial-body pick becomes a whole-body pick once you've drilled all the way in.
    No-op when the whole body (or a non-body entity) is already selected."""
    try:
        if isinstance(entity, adsk.fusion.BRepBody):
            return
        body = getattr(entity, 'body', None)
        if not isinstance(body, adsk.fusion.BRepBody):
            return
        sel = _ui.activeSelections
        sel.clear()
        sel.add(body)
    except Exception:
        pass


# ----------------------------
# DESCENT
# ----------------------------
def _activate_level(design, current_occ, target_next, kept):
    """Activate one level deeper (target_next) and re-select the kept geometry."""
    fvs.take_snapshot(design, current_occ)
    if fvs.isolate_is_active():
        fvs.isolate_occurrence_no_snapshot(design, target_next)
    _ui.activeSelections.clear()
    target_next.activate()
    fvs.remember_activation(target_next)
    fvs.apply_policy(design)
    _reselect(kept)


def _enter_sketch_edit(design, sketch, sketch_occ, kept):
    """Enter edit mode for `sketch`.  We are already active at the sketch's
    parent-component instance, so (unlike the old all-in-one Go Down) there is
    no definition-vs-instance camera offset to correct."""
    fvs.clear_sketch_cam()  # ensure Go Up/Top won't apply a stale delta on exit
    if sketch_occ:
        fvs.remember_sketch_entry_occ(sketch_occ)
    if not fvs.is_editing_sketch(design):
        fvs.remember_pre_sketch_marker(design)
    # Record which sketch we're about to edit so the visibility policy can keep
    # it visible even when design.activeEditObject later raises (sketch nested in
    # an occurrence).  Identity from activeEditObject is unreliable at depth.
    try:
        fvs.remember_edit_sketch(sketch)
    except Exception:
        pass
    sel = _ui.activeSelections
    sel.clear()
    sel.add(sketch)
    edit = _ui.commandDefinitions.itemById('SketchActivate')
    if edit:
        edit.execute()


# ----------------------------
# EXECUTE HANDLER
# ----------------------------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                return
            selections = _ui.activeSelections

            # ── CASE 1: inside sketch edit → extend to connected curves ──────
            # fvs, not activeEditObject: that property raises for a sketch nested in
            # an occurrence, which aborted this whole handler — so "extend to connected
            # curves" did nothing at all inside any sub-component's sketch.
            active_sketch = fvs.active_edit_sketch(design)
            if active_sketch:
                if selections.count == 0:
                    return
                seed = [selections.item(i).entity for i in range(selections.count)]
                connected = _connected_sketch_curves(active_sketch, seed)
                if connected:
                    selections.clear()
                    for curve in connected:
                        try:
                            selections.add(curve)
                        except Exception:
                            pass
                return

            if selections.count == 0:
                return

            # Capture the current selection so we can re-apply it after activating.
            kept = [selections.item(i).entity for i in range(selections.count)]
            entity = kept[0]
            sketch = get_sketch(entity)
            target_occ = get_occurrence(entity)

            # Geometry that lives at root level (no occurrence) — only a sketch
            # in the active root component can still be entered for editing.
            if target_occ is None:
                if sketch and sketch.parentComponent == design.activeComponent:
                    _enter_sketch_edit(design, sketch, None, kept)
                elif not sketch:
                    _promote_partial_body(entity)
                return

            # Full root→geometry occurrence path.
            _, path_occs = fvs._find_occ_info(design, target_occ)
            if not path_occs:
                return

            # Current active occurrence (None ⇒ root active).
            active_comp = design.activeComponent
            A = fvs.recall_active_occ(design)
            if not (A and A.component == active_comp):
                A = fvs.find_occurrence(design, active_comp)
            active_path = fvs._occ_path_str(A) if A else ''

            # Deepest path node that is an ancestor of (or equal to) the active
            # occurrence; the next node down the path is what we activate.
            k = -1
            for i, occ in enumerate(path_occs):
                if active_path and _is_prefix(fvs._occ_path_str(occ), active_path):
                    k = i
            nxt = k + 1
            # Step over pass-through wrappers, so one press lands in the next component
            # the browser actually shows (see fvs.is_disregarded).
            while nxt < len(path_occs) and fvs.is_disregarded(path_occs[nxt]):
                nxt += 1

            if nxt >= len(path_occs):
                # Already at the geometry's parent component (bottom of descent).
                if sketch:
                    _enter_sketch_edit(design, sketch, target_occ, kept)
                else:
                    _promote_partial_body(entity)
                return

            _activate_level(design, A, path_occs[nxt], kept)

        except Exception:
            if _ui:
                _ui.messageBox('Go Down a Level failed:\n{}'.format(traceback.format_exc()))


# ----------------------------
# COMMAND CREATED HANDLER
# ----------------------------
class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            on_execute = ExecuteHandler()
            args.command.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            if _ui:
                _ui.messageBox('CommandCreated failed:\n{}'.format(traceback.format_exc()))


# ----------------------------
# CONNECTED SKETCH CURVES (unchanged feature)
# ----------------------------
def _connected_sketch_curves(sketch, seed_entities):
    all_curves = [sketch.sketchCurves.item(i) for i in range(sketch.sketchCurves.count)]

    def _point_tokens(curve):
        tokens = []
        for attr in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
            try:
                pt = getattr(curve, attr, None)
                if pt:
                    tokens.append(pt.entityToken)
            except Exception:
                pass
        return tokens

    pt_to_curves = {}
    for curve in all_curves:
        for tok in _point_tokens(curve):
            pt_to_curves.setdefault(tok, []).append(curve)

    queue = []
    for entity in seed_entities:
        profile_loops = getattr(entity, 'profileLoops', None)
        if profile_loops is not None:
            for loop in profile_loops:
                for pc in loop.profileCurves:
                    try:
                        se = pc.sketchEntity
                        if se is not None:
                            queue.append(se)
                    except Exception:
                        pass
        elif isinstance(entity, adsk.fusion.SketchCurve):
            queue.append(entity)
        elif isinstance(entity, adsk.fusion.SketchPoint):
            try:
                for neighbor in pt_to_curves.get(entity.entityToken, []):
                    queue.append(neighbor)
            except Exception:
                pass

    visited = set()
    result = []
    while queue:
        curve = queue.pop(0)
        try:
            tok = curve.entityToken
        except Exception:
            continue
        if tok in visited:
            continue
        visited.add(tok)
        result.append(curve)
        for pt_tok in _point_tokens(curve):
            for neighbor in pt_to_curves.get(pt_tok, []):
                try:
                    if neighbor.entityToken not in visited:
                        queue.append(neighbor)
                except Exception:
                    pass

    try:
        for profile in sketch.profiles:
            found = False
            for loop in profile.profileLoops:
                if found:
                    break
                for pc in loop.profileCurves:
                    try:
                        se = pc.sketchEntity
                        if se and se.entityToken in visited:
                            result.append(profile)
                            found = True
                            break
                    except Exception:
                        pass
    except Exception:
        pass

    return result


# ----------------------------
# RUN / STOP
# ----------------------------
def run(context):
    global _app, _ui, _doc_snapshot_handler
    global _sketch_activate_listener, _sketch_activate_starting_listener
    global _suspend_starting_listener

    _app = adsk.core.Application.get()
    _ui = _app.userInterface

    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        _sketch_activate_listener = SketchActivateTerminatedListener()
        _ui.commandTerminated.add(_sketch_activate_listener)
        _handlers.append(_sketch_activate_listener)

        _sketch_activate_starting_listener = SketchActivateStartingListener()
        _ui.commandStarting.add(_sketch_activate_starting_listener)
        _handlers.append(_sketch_activate_starting_listener)

        _suspend_starting_listener = SuspendStartingListener()
        _ui.commandStarting.add(_suspend_starting_listener)
        _handlers.append(_suspend_starting_listener)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if design:
                fvs.take_snapshot(design, None)
        except Exception:
            pass

        _doc_snapshot_handler = _DocSnapshotHandler()
        _app.documentOpened.add(_doc_snapshot_handler)

    except Exception:
        if _ui:
            _ui.messageBox('Go Down a Level run() failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _doc_snapshot_handler
    try:
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()

        if _sketch_activate_listener:
            _ui.commandTerminated.remove(_sketch_activate_listener)
        if _sketch_activate_starting_listener:
            _ui.commandStarting.remove(_sketch_activate_starting_listener)
        if _suspend_starting_listener:
            _ui.commandStarting.remove(_suspend_starting_listener)
        if _doc_snapshot_handler:
            _app.documentOpened.remove(_doc_snapshot_handler)
            _doc_snapshot_handler = None

        _handlers.clear()
    except Exception:
        pass
