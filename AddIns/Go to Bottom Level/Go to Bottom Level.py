"""
Go to Bottom Level
──────────────────
Activates all the way down to the parent component of the selected geometry in
one press, keeping the geometry selected the whole way.  This is exactly what
holding "Go Down a Level" would do — it loops the same single-level descent —
so the per-level snapshots / isolation stay consistent with incremental Go Down.
For a selected sketch it finishes by entering sketch edit (the true bottom).

(Replaces the Shift+click jump that the retired Auto-Activate Component add-in
used to provide.)
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

CMD_ID   = 'GoToBottomLevelCmd'
CMD_NAME = 'Go to Bottom Level'
CMD_TIP  = 'Activate down to the parent component of the selected geometry'
PANEL_ID = 'SolidScriptsAddinsPanel'

_app = None
_ui = None
_handlers = []

# NOTE: this add-in used to run its OWN global commandTerminated/commandStarting
# listeners for SketchActivate/SketchStop (a "mirror Go Down" pair that called
# remember_pre_sketch_marker / restore_pre_sketch_marker independently).  Go Down
# a Level's own listener already does this — it is explicitly entry-method-
# independent, firing for EVERY sketch edit regardless of which command opened
# it (double-click, this add-in's own SketchActivate.execute(), Go Down itself).
# Having a SECOND global listener for the same events raced it: whichever fired
# first rolled the timeline marker forward via restore_pre_sketch_marker, and
# if that happened before Go Down's listener read edited_sketch_from_marker,
# Go Down identified the WRONG sketch (whatever timeline entry happened to sit
# at the marker's resting position) — which then hid the sketch actually being
# edited instead of showing it.  Removed rather than kept in sync with Go Down,
# since one authority for this is the whole point.


def get_sketch(entity):
    if isinstance(entity, adsk.fusion.Sketch):
        return entity
    if hasattr(entity, 'parentSketch') and entity.parentSketch:
        return entity.parentSketch
    return None


def get_occurrence(entity):
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


def _activate_level(design, current_occ, target_next, kept):
    fvs.take_snapshot(design, current_occ)
    if fvs.isolate_is_active():
        fvs.isolate_occurrence_no_snapshot(design, target_next)
    _ui.activeSelections.clear()
    target_next.activate()
    fvs.remember_activation(target_next)
    fvs.apply_policy(design)
    _reselect(kept)


def _enter_sketch_edit(design, sketch, sketch_occ, kept):
    fvs.clear_sketch_cam()
    if sketch_occ:
        fvs.remember_sketch_entry_occ(sketch_occ)
    if not fvs.is_editing_sketch(design):
        fvs.remember_pre_sketch_marker(design)
    sel = _ui.activeSelections
    sel.clear()
    sel.add(sketch)
    edit = _ui.commandDefinitions.itemById('SketchActivate')
    if edit:
        edit.execute()


class SuspendStartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Suspend mode (fvs § Suspend mode: peek): all the way down to the selection's
    component, as a peek, run from the veto — nothing starts, nothing is entered."""
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
                fvs.peek_toward(design, get_occurrence(sel.item(0).entity), full=True)
        except Exception:
            pass


# ── execute ──────────────────────────────────────────────────────────────────
class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                return
            # Already editing a sketch — nothing below.
            if fvs.is_editing_sketch(design):
                return

            sel = _ui.activeSelections
            if sel.count == 0:
                return
            kept = [sel.item(i).entity for i in range(sel.count)]
            entity = kept[0]
            sketch = get_sketch(entity)
            target_occ = get_occurrence(entity)

            if target_occ is None:
                if sketch and sketch.parentComponent == design.activeComponent:
                    _enter_sketch_edit(design, sketch, None, kept)
                return

            _, path_occs = fvs._find_occ_info(design, target_occ)
            if not path_occs:
                return

            # Loop the single-level descent until we reach the parent component.
            guard = 0
            while guard < 256:
                guard += 1
                active_comp = design.activeComponent
                A = fvs.recall_active_occ(design)
                if not (A and A.component == active_comp):
                    A = fvs.find_occurrence(design, active_comp)
                active_path = fvs._occ_path_str(A) if A else ''
                k = -1
                for i, occ in enumerate(path_occs):
                    if active_path and _is_prefix(fvs._occ_path_str(occ), active_path):
                        k = i
                nxt = k + 1
                if nxt >= len(path_occs):
                    break
                _activate_level(design, A, path_occs[nxt], kept)

            # At the parent component now — for a sketch, finish into edit mode.
            if sketch:
                _enter_sketch_edit(design, sketch, target_occ, kept)

        except Exception:
            if _ui:
                _ui.messageBox('Go to Bottom Level failed:\n{}'.format(traceback.format_exc()))


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


def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        on_starting = SuspendStartingHandler()
        _ui.commandStarting.add(on_starting)
        _handlers.append(on_starting)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        if _ui:
            _ui.messageBox('Go to Bottom Level run() failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        for h in _handlers:
            if isinstance(h, SuspendStartingHandler):
                try:
                    _ui.commandStarting.remove(h)
                except Exception:
                    pass
        _handlers.clear()
    except Exception:
        pass
