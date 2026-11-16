"""
Delete Selection
────────────────
One command whose only job is to be a second key for Fusion's own Delete, so
Cmd+D deletes whatever is selected — body, component, sketch, sketch curve,
face, feature, construction geometry — exactly as the Delete key does.

WHY A WRAPPER AND NOT JUST A SECOND BINDING
───────────────────────────────────────────
The command that the Delete key runs is `DeleteCommand`, and its delete /
backspace bindings are Fusion DEFAULTS.  Fusion's only write path for the
keymap is its own "Change Keyboard Shortcut" prompt (HotKey.Dialog — see the
Shortcuts add-in), and that prompt MOVES a command's binding rather than adding
one: measured live, assigning this command a second key silently dropped the key
it already had.  Aimed at DeleteCommand it would therefore have cost the Delete
key itself — a bad trade — so Cmd+D goes on this command id instead: a brand-new
id with no binding to disturb, which forwards to DeleteCommand.

Forwarding (rather than deleting through the API) is deliberate: Fusion's delete
is context-dispatched — the right delete for a sketch curve in sketch edit, for a
face, for an occurrence in the browser — and it handles the timeline and undo the
way pressing Delete does.

WHICH delete, though, depends on the ENVIRONMENT.  `DeleteCommand` (the Delete
key's command) is disabled outside Design: in the Render workspace — which is
where Josh models — firing it does nothing at all, silently.  `FusionDeleteCommand`
is the one enabled there.  So the command is chosen at fire time by asking
controlDefinition.isEnabled, rather than being hard-coded (_delete_command).

THE SELECTION HAS TO BE PUT BACK FIRST
──────────────────────────────────────
Fusion clears the active selection as this command terminates, and a command
started with execute() only runs once the current one has ended — so firing
DeleteCommand from the execute handler races that clear and, measured live,
usually loses: DeleteCommand starts with nothing selected and silently does
nothing.  So the selection is snapshotted in execute, re-applied in
commandTerminated (after the clear), and only then is DeleteCommand fired.

This add-in covers the VIEWPORT (and Fusion's own browser).  The companion
windows own Cmd+D themselves, because their cursor/selection is window-local and
is not a Fusion selection — a forwarded hotkey would delete whatever the viewport
last had selected, usually nothing.  Browser: Cmd+D deletes the rows it is on
(action "delete" → _delete_items in Browser.py).  Params: Cmd+D deletes the
selected parameter.  Both consume the key before the forwarding step.
"""

import adsk.core, adsk.fusion, os, traceback, types as _types

CMD_ID   = 'DeleteSelectionCmd'
CMD_NAME = 'Delete Selection'
CMD_TIP  = 'Delete whatever is currently selected (same as the Delete key)'
PANEL_ID = 'SolidScriptsAddinsPanel'

# Loaded fresh from disk, same exec-from-path pattern Browser.py uses — only for
# sketch_parts/delete_parts, so a sketch deleted here also loses the plane the box
# built for it (Browser.py's own _delete_items does the same). Never a reason for
# this add-in not to start.
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
try:
    bnds = _load_local('bnds', 'Bounds.py')
except Exception:
    bnds = None
# The one whole-item delete Browser, Timeline and this add-in share.
deleting = _load_local('deleting', 'Deleting.py')
# A Browser sketch pick is its curves + the Sketch — one sketch to delete.
skp = _load_local('skp', 'Sketch Picks.py')

# Render-workspace mirror (see _mirror_into_render). Tab id matches the one the
# Timeline Toggle add-in builds, so this lands in the same hidden mirror.
RENDER_WS_ID  = 'FusionRenderEnvironment'
RENDER_TAB_ID = 'DesignCmdsTab'
RENDER_PANEL_ID = 'DC_DeleteSelection'

# The delete the Delete key runs. It is DISABLED outside the Design environment —
# and Josh models in the Render workspace, where it is inert (measured: enabled
# False there with a sketch selected, while FusionDeleteCommand was True). So the
# command that actually runs is whichever of these is enabled at the time.
DELETE_CMD_IDS = ('DeleteCommand', 'FusionDeleteCommand')

_app = None
_ui = None
_handlers = []
_terminated_listener = None
_starting_listener = None

_pending = []          # entities snapshotted in execute, re-selected on terminate


def _mirror_into_render(cmd_def):
    """Make this command reachable in the RENDER workspace.

    A shortcut only dispatches to a command the current environment carries, and
    Josh models in Render — which is why Timeline Toggle mirrors every Design
    command into hidden Render panels.  But that mirror is taken once, when
    Timeline Toggle starts: a command registered later — by an add-in that loads
    after it, which is load-order luck — is simply absent from Render, and its
    hotkey then does NOTHING there, silently.  Measured: Cmd+D worked in Design
    and was inert in Render until this ran.  So register ourselves rather than
    depend on that ordering.
    """
    ws = _ui.workspaces.itemById(RENDER_WS_ID)
    if not ws:
        return
    tab = (ws.toolbarTabs.itemById(RENDER_TAB_ID)
           or ws.toolbarTabs.add(RENDER_TAB_ID, 'DesignCmds'))
    panel = (tab.toolbarPanels.itemById(RENDER_PANEL_ID)
             or tab.toolbarPanels.add(RENDER_PANEL_ID, 'DCDel'))
    if not panel.controls.itemById(CMD_ID):
        panel.controls.addCommand(cmd_def)


# Rows the API can remove outright, no command and no dialog involved.
_API_DELETABLE = (adsk.fusion.Occurrence, adsk.fusion.BRepBody, adsk.fusion.Sketch,
                  adsk.fusion.ConstructionPlane, adsk.fusion.ConstructionAxis,
                  adsk.fusion.ConstructionPoint)


def _api_delete(entities):
    """Delete whole items straight through the API.  Returns False (having done
    nothing) if the selection holds anything this can't handle.

    Why not always use the Delete command: in the Render workspace the only
    enabled delete is FusionDeleteCommand, and for a component it opens a DELETE
    dialog and waits for OK — with the UI chrome hidden that reads as "the hotkey
    did nothing".  Components, bodies and sketches — the whole-item deletes — go
    straight through deleteMe() instead, so the item just disappears.  Anything
    else (a face, an edge, a sketch curve, a feature) still goes to the command,
    which is the only thing that knows how to delete those.
    """
    if not entities or not all(isinstance(e, _API_DELETABLE) for e in entities):
        return False

    # A sketch the plane box placed carries the construction geometry it built (offset
    # plane / pivot axis / angled plane) in attributes on the sketch — sketch_parts
    # reads those. Worked out BEFORE the delete: same reason Browser.py's own
    # _delete_items does this first, there is nothing left to ask afterwards.
    plane_parts = []
    if bnds is not None:
        try:
            design = adsk.fusion.Design.cast(_app.activeProduct)
        except Exception:
            design = None
        if design is not None:
            for e in entities:
                if not isinstance(e, adsk.fusion.Sketch):
                    continue
                try:
                    native = e.nativeObject or e
                    parts = bnds.sketch_parts(design, native)
                    if parts:
                        plane_parts.append(parts)
                except Exception:
                    pass

    # The one whole-item delete Browser and Timeline share too (Deleting.py — read its
    # header): a sketch with timeline after it goes to the Delete command instead of
    # deleteMe(), which froze for up to 2 min here and then deleted nothing.
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        deleting.delete(_ui, design, entities)
    except Exception:
        deleting.log('  Delete Selection: delete raised', traceback.format_exc().replace('\n', ' | '))
        return False

    for parts in plane_parts:
        try:
            bnds.delete_parts(parts)
        except Exception:
            pass
    return True


def _delete_command():
    """The delete Fusion will actually run in the current environment, or None."""
    for cid in DELETE_CMD_IDS:
        cd = _ui.commandDefinitions.itemById(cid)
        try:
            if cd and cd.controlDefinition.isEnabled:
                return cd
        except Exception:
            pass
    return None


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            sel = _ui.activeSelections
            # Never None: activeSelections.add(None) is a native null deref — Fusion
            # dies outright, no Python exception (see _Starting).
            _pending[:] = [e for e in (sel.item(i).entity for i in range(sel.count))
                           if e is not None]
            deleting.log('Space A: Delete Selection execute, sel=%d' % sel.count)
        except Exception:
            if _ui:
                _ui.messageBox('Delete Selection failed:\n{}'.format(traceback.format_exc()))


class _Starting(adsk.core.ApplicationCommandEventHandler):
    """Some selectable things have no API object: a coincident constraint in a
    sketch reads back as Selection.entity == None (measured 2026-09-30, Sketch16 of
    260612). Such a selection can't be snapshotted and put back — re-adding the None
    crashed Fusion on every Space A. So when the live selection holds one, this
    command is cancelled BEFORE it starts (so it never ends, and Fusion never clears
    the selection) and the Delete command is fired on the selection as it stands."""
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            if args.commandId in DELETE_CMD_IDS:
                deleting.log('  %s starting, sel=%d' % (args.commandId, _ui.activeSelections.count))
            if args.commandId != CMD_ID:
                return
            sel = _ui.activeSelections
            if not any(sel.item(i).entity is None for i in range(sel.count)):
                return                      # normal path: execute + _Terminated
            args.isCancelled = True
            cd = _delete_command()
            if cd:
                cd.execute()
        except Exception:
            pass


class _Terminated(adsk.core.ApplicationCommandEventHandler):
    """Fires after this command has ended — i.e. after Fusion has cleared the
    selection — so the snapshot can be put back and handed to Delete."""
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            if args.commandId in DELETE_CMD_IDS:
                deleting.log('  %s terminated, reason=%s' % (args.commandId, args.terminationReason))
            if args.commandId != CMD_ID:
                return
            kept, _pending[:] = skp.whole_sketches(_pending), []
            deleting.log('Space A: Delete Selection terminated, kept=%d' % len(kept),
                         [type(e).__name__ for e in kept[:6]])
            if not kept:
                return                      # nothing was selected — stay silent
            if _api_delete(kept):
                return          # never clear here: a queued Delete command reads the selection
            sel = _ui.activeSelections
            sel.clear()
            for e in kept:
                try:
                    sel.add(e)
                except Exception:
                    pass                    # entity died with an earlier one in the set
            if sel.count == 0:
                return
            cd = _delete_command()      # checked with the selection back in place
            if cd:
                cd.execute()
        except Exception:
            if _ui:
                _ui.messageBox('Delete Selection failed:\n{}'.format(traceback.format_exc()))


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
    global _app, _ui, _terminated_listener, _starting_listener
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        _terminated_listener = _Terminated()
        _ui.commandTerminated.add(_terminated_listener)
        _handlers.append(_terminated_listener)

        _starting_listener = _Starting()
        _ui.commandStarting.add(_starting_listener)
        _handlers.append(_starting_listener)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        _mirror_into_render(cmd_def)
    except Exception:
        if _ui:
            _ui.messageBox('Delete Selection run() failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        for pid in (PANEL_ID, RENDER_PANEL_ID):
            panel = _ui.allToolbarPanels.itemById(pid)
            if panel:
                ctrl = panel.controls.itemById(CMD_ID)
                if ctrl:
                    ctrl.deleteMe()
        if _terminated_listener:
            _ui.commandTerminated.remove(_terminated_listener)
        if _starting_listener:
            _ui.commandStarting.remove(_starting_listener)
        _handlers.clear()
        # Do NOT delete the command definition — that would drop the Cmd+D binding.
    except Exception:
        pass
