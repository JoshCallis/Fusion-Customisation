"""The ONE way the add-ins delete a whole item (occurrence, body, sketch, construction
geometry). Browser (_delete_items), Timeline (_delete) and Delete Selection (the
viewport's Cmd+D / Space A) all call delete() here.

WHY ONE MODULE
--------------
Each of those three used to carry its own copy of this logic, and every fix landed in
only one of them. The 2026-09-28 mid-timeline-sketch fix went into the Browser only, so
Space A with the Browser WINDOW focused worked, and Space A with FUSION focused (which
runs Delete Selection) still froze and deleted nothing — "fixed for a bit, then back".
Change the rule here and all three get it.

THE ROUTE: TimelineObject.deleteObject()
----------------------------------------
Anything that owns a timeline item (sketch, construction geometry, feature) is deleted
through its timeline object — the API twin of the timeline's own right-click Delete.
Measured 2026-10-08 on 260612, Sketch37 at 384/389: deleteObject() removed it in 0.64 s.
Checked by re-resolving the entityToken (the only test that survives a replay).

WHY NOT THE TWO ROUTES THIS USED TO TAKE (the "fixed a million times" history)
-------------------------------------------------------------------------------
1. Sketch.deleteMe() on a sketch with timeline after it rolls back, replays (9-112 s on
   260612), drops the change and RETURNS TRUE; the replay kills the handle so isValid
   reads False and the delete LOOKS done.
2. Fusion's DeleteCommand on an API-selected sketch — the 09-28 "fix" for (1) — is a
   silent no-op too. Measured 2026-10-08: it starts with sel=1, terminates Completed in
   8 ms, and the sketch stays in the timeline (the first run even left it half-gone:
   dropped from component.sketches, still in the timeline, token dead). Every earlier
   fix tuned WHICH caller reached this command; none checked that it deletes anything.
Both are now only fallbacks (bodies/occurrences have no timeline item of their own:
deleteMe(); and anything deleteObject() refuses), and every delete is logged to LOG
with what actually happened — read it first next time.

The Delete command acts on the selection and runs AFTER the caller's tick, so callers
must not clear the selection after delete() returns with commanded > 0.
"""

import adsk.core, adsk.fusion, time

LOG = '/tmp/fusion_delete.log'


def log(*parts):
    """Every delete path writes here — the one place to look when a delete 'does nothing'."""
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _desc(e):
    try:
        return '%s %r' % (type(e).__name__, getattr(e, 'name', ''))
    except Exception:
        return type(e).__name__


def _ws(ui):
    try:
        return ui.activeWorkspace.id
    except Exception:
        return '?'


def _find(design, tok):
    if not tok:
        return None
    try:
        found = design.findEntityByToken(tok)
        return found[0] if found and len(found) else None
    except Exception:
        return None


def has_timeline_after(design, ent):
    """Is there anything in the timeline after this entity's own item?"""
    try:
        to = ent.timelineObject
        return to is not None and to.index < design.timeline.count - 1
    except Exception:
        return False


def delete_by_command(ui, design, ents):
    """Fusion's own Delete on these. Only an in-context entity can be selected (a native
    sketch in a sub-component raises "invalid argument entity"), so a native one is put
    in its component's first occurrence. In Design it is DeleteCommand (its isEnabled
    reads False right after the selection change, yet it runs); elsewhere only
    FusionDeleteCommand is live. Returns how many were selected for it."""
    sel = ui.activeSelections
    sel.clear()
    for e in ents:
        try:
            if (not isinstance(e, adsk.fusion.Occurrence) and e.assemblyContext is None
                    and e.parentComponent != design.rootComponent):
                occs = design.rootComponent.allOccurrencesByComponent(e.parentComponent)
                if occs.count:
                    e = e.createForAssemblyContext(occs.item(0))
            sel.add(e)
        except Exception as ex:
            log('  select failed', _desc(e), ex)
    if not sel.count:
        log('  by command: nothing selectable')
        return 0
    try:
        design_ws = ui.activeWorkspace.id == 'FusionSolidEnvironment'
    except Exception:
        design_ws = True
    cd = ui.commandDefinitions.itemById('DeleteCommand' if design_ws else 'FusionDeleteCommand')
    log('  by command', cd.id if cd else None, 'sel=%d' % sel.count,
        'enabled=%s' % (cd.controlDefinition.isEnabled if cd else None),
        'active=%r' % ui.activeCommand)
    if cd:
        cd.execute()
    return sel.count


def _native(e):
    if isinstance(e, adsk.fusion.Occurrence):
        return e
    try:
        return e.nativeObject or e                # a proxy can't be deleted; its native can
    except Exception:
        return e


def _token(e):
    try:
        return e.entityToken
    except Exception:
        return ''


def _timeline_delete(design, target, tok):
    """True if target's own timeline item was deleted (and the token no longer resolves).
    Occurrences and bodies are excluded: an occurrence's timeline item is its component's
    creation, and a body has none of its own (its deleteMe() adds a Remove feature)."""
    if isinstance(target, (adsk.fusion.Occurrence, adsk.fusion.BRepBody)):
        return False
    try:
        to = target.timelineObject
    except Exception:
        to = None
    if to is None:
        return False
    try:
        if not to.isDeletable:
            log('  timeline item not deletable', _desc(target))
            return False
        t0 = time.time()
        ok = to.deleteObject()
        gone = not tok or _find(design, tok) is None
        log('  deleteObject', _desc(target), 'ok=%s gone=%s %.2fs' % (ok, gone, time.time() - t0))
        return gone
    except Exception as ex:
        log('  deleteObject raised', _desc(target), ex)
        return False


def delete(ui, design, ents):
    """Delete these whole items. Returns (api_deleted, commanded): how many the API
    really removed, and how many were handed to the Delete command (queued)."""
    log('delete', [_desc(e) for e in ents], 'ws=%s' % _ws(ui), 'active=%r' % ui.activeCommand)
    ui.activeSelections.clear()
    # Tokens first: a delete can replay the design and kill every other handle in ents.
    items = [(e, _token(_native(e))) for e in ents]
    refused, done = [], 0
    for e, tok in items:
        try:
            alive = e.isValid
        except Exception:
            alive = False
        if not alive:
            e = _find(design, tok)
            if e is None:
                done += 1                         # went with an earlier one (its parent)
                continue
        target = _native(e)
        if _timeline_delete(design, target, tok):
            done += 1
            continue
        if isinstance(target, adsk.fusion.Sketch) and has_timeline_after(design, target):
            log('  timeline after', _desc(target), '→ command')   # deleteMe would freeze
            refused.append(e)
            continue
        try:
            target.deleteMe()
        except Exception:
            pass                                  # already gone with its parent
        if tok and _find(design, tok) is not None:
            log('  deleteMe left', _desc(target), '→ command')
            refused.append(e)
        else:
            done += 1
    commanded = delete_by_command(ui, design, refused) if refused else 0
    log('  done=%d commanded=%d' % (done, commanded))
    return done, commanded
