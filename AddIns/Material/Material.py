"""
Material — 3ds Max's Eyedropper / Apply Last Material, in Fusion
(max/callis/material.py is the Max twin; keep the two behaving alike).

    Pick Material   (PickMaterialCmd)
        something selected → what it wears (a face: what that face shows; a body or an
        occurrence: what the body shows). Nothing selected → the next click on a body
        (a command of our own with one selection input and its palette hidden; Esc
        cancels). The cursor is Fusion's ordinary one throughout.
    Apply Material  (ApplyMaterialCmd)
        the last material onto WHOLE bodies: selected bodies, a selected face's body,
        every body under a selected occurrence. The native body, so every instance —
        the Browser palette's rule. One undo step.

Both are silent: no message, no HUD, no dialog — the click is its own confirmation.

THE LAST MATERIAL
─────────────────
What Pick took, OR the latest appearance assigned any other way:
  • a Browser swatch dropped on a row        Browser.py _paint → set_last
  • Fusion's own Appearance dialog / Undo     Dim Outside Active's pass notices a visible
                                              body now wearing a different original
                                              (_noticed) → set_last
Kept as {name, id} in last.json beside this file (so a reload or a document switch
keeps it), plus the live object while it stays valid. Resolved into the active design
by name + id; missing there, copied in from the Fusion Appearance Library by id.

A dim copy (Dim Outside Active's "· dim" / "· tone N") is never the last material — it
is mapped back to the appearance it was made from (hook.undimmed).

Never writes an appearance's properties — only assigns (a write to a used appearance
froze Fusion 2026-09-30).

Log: /tmp/fusion_material.log
"""

import json
import os
import sys
import time
import traceback
import importlib.util

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface

handlers = []

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
LAST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last.json')
LOG = '/tmp/fusion_material.log'

PICK_ID = 'PickMaterialCmd'
PICK_NAME = 'Pick Material'
PICK_DESC = ('Take the material (appearance) of the selected body or face — or, with '
             'nothing selected, of the next body clicked — for Apply Material.')
APPLY_ID = 'ApplyMaterialCmd'
APPLY_NAME = 'Apply Material'
APPLY_DESC = ('Put the last picked or assigned material on the selected bodies (a face '
              'paints its whole body, a component every body in it).')
PICKING_ID = 'PickMaterialClickCmd'      # the click-to-pick command Pick starts
OWNER = 'Material'
PANEL_ID = 'SolidScriptsAddinsPanel'
HOOK = 'material_hook'
DIM_HOOK = 'dim_outside_active_hook'
END_EVENT = 'JoshMaterialPickEnd'
LIB_ID = 'BA5EE55E-9982-449B-9D66-9F036540E140'   # Fusion Appearance Library


def _load_local(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = type(sys)(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


tickguard = _load_local('tickguard', 'Tick Guard.py')


def _log(*a):
    try:
        with open(LOG, 'a') as f:
            f.write('%s  %s\n' % (time.strftime('%H:%M:%S'), ' '.join(str(x) for x in a)))
    except Exception:
        pass


def _hotkeys():
    """The shared action registry — Extrude.py's _hotkeys (ONE registry via sys.modules,
    hot-reloaded on the file's mtime)."""
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


def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


# ── The last material ────────────────────────────────────────────────────────

_last = {'name': '', 'id': '', 'obj': None}


def _read_last():
    try:
        with open(LAST_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        _last.update(name=d.get('name', ''), id=d.get('id', ''), obj=None)
    except Exception:
        pass


def _undimmed(a):
    """The appearance a dim copy was made from; anything else as it is."""
    hook = sys.modules.get(DIM_HOOK)
    design = _design()
    if a is None or hook is None or design is None or not hasattr(hook, 'undimmed'):
        return a
    try:
        return hook.undimmed(design, a) or a
    except Exception:
        return a


def set_last(a):
    """Make `a` the last material. Called by Pick, the Browser palette and Dim Outside
    Active's pass (an assignment made any other way)."""
    try:
        a = _undimmed(a)
        if a is None or not a.isValid:
            return
        if _last['obj'] is not None and _last['obj'] == a:
            return
        _last.update(name=a.name, id=a.id, obj=a)
        with open(LAST_FILE, 'w', encoding='utf-8') as f:
            json.dump({'name': a.name, 'id': a.id}, f)
        _log('last =', a.name, a.id)
    except Exception:
        _log('set_last failed:', traceback.format_exc().replace('\n', ' | '))


def _resolve(design):
    """The last material as an appearance of `design` (copied in from the Library if
    the design hasn't got it), or None."""
    a = _last['obj']
    try:
        if a is not None and a.isValid and any(x == a for x in design.appearances):
            return a
    except Exception:
        pass
    name, aid = _last['name'], _last['id']
    if not name:
        return None
    for x in design.appearances:
        if x.name == name and x.id == aid:
            _last['obj'] = x
            return x
    lib = app.materialLibraries.itemById(LIB_ID)
    src = None
    try:
        src = lib.appearances.itemById(aid) if lib else None
    except Exception:
        src = None
    if src is None and lib is not None:
        src = lib.appearances.itemByName(name)
    if src is None:
        _log('no', name, aid, 'in the design or the Library')
        return None
    x = design.appearances.addByCopy(src, name)
    _last['obj'] = x
    return x


# ── What a selection means ───────────────────────────────────────────────────

def _selection():
    try:
        sel = ui.activeSelections
        return [sel.item(i).entity for i in range(sel.count)]
    except Exception:
        return []


def _worn(ent):
    """What the clicked thing shows: a face its own look, a body or an occurrence's
    first body theirs."""
    f = adsk.fusion.BRepFace.cast(ent)
    if f:
        return f.appearance
    b = adsk.fusion.BRepBody.cast(ent)
    if b:
        return b.appearance
    o = adsk.fusion.Occurrence.cast(ent)
    if o:
        bodies = list(o.bRepBodies)
        if bodies:
            return bodies[0].appearance
        return o.appearance
    return None


def _bodies(ents):
    """Native bodies for the selection — whole bodies always."""
    out, seen = [], set()

    def add(b):
        b = b.nativeObject or b
        tok = b.entityToken
        if tok not in seen:
            seen.add(tok)
            out.append(b)

    for e in ents:
        b = adsk.fusion.BRepBody.cast(e)
        if b:
            add(b)
            continue
        f = adsk.fusion.BRepFace.cast(e)
        if f:
            add(f.body)
            continue
        o = adsk.fusion.Occurrence.cast(e)
        if o:
            comps = [o.component] + [s.component for s in o.component.allOccurrences]
            for c in comps:
                for b in c.bRepBodies:
                    add(b)
    return out


# ── Commands ─────────────────────────────────────────────────────────────────

def _pick():
    sel = _selection()
    if not sel:
        cd = ui.commandDefinitions.itemById(PICKING_ID)
        if cd:
            cd.execute()
        return
    worn = [a for a in (_worn(e) for e in sel) if a is not None]
    if worn:
        set_last(worn[0])


def _apply():
    design = _design()
    if design is None:
        return
    bodies = _bodies(_selection())
    if not bodies:
        return
    with tickguard.one_step('Material'):
        a = _resolve(design)
        if a is None:
            return
        hook = sys.modules.get(DIM_HOOK)
        if hook is not None and hasattr(hook, 'paint'):
            hook.paint(bodies, a)
        else:
            for b in bodies:
                b.appearance = a
    _log('applied', a.name, 'to', len(bodies), 'bodies')


_RUN = {PICK_ID: _pick, APPLY_ID: _apply}


class _Starting(adsk.core.ApplicationCommandEventHandler):
    """Pick and Apply veto themselves and do the work here — Snap View's pattern — so a
    running command isn't ended and the selection survives."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
            fn = _RUN.get(args.commandId)
            if fn is None:
                return
            args.isCanceled = True
            fn()
        except Exception:
            _log('failed:', traceback.format_exc().replace('\n', ' | '))


class _Created(adsk.core.CommandCreatedEventHandler):
    """Only reached if the veto didn't fire (a toolbar press with the handler gone)."""
    def __init__(self, cid):
        super().__init__()
        self.cid = cid

    def notify(self, args):
        try:
            if self in handlers:
                _RUN[self.cid]()
        except Exception:
            _log('created failed:', traceback.format_exc().replace('\n', ' | '))


# ── Click to pick ────────────────────────────────────────────────────────────
# A command with one body selection input and no palette: the first body clicked is
# taken and the command ends itself (through a custom event — never from inside the
# input's own event).

_picking = {'cmd': None}


def _hide_palette(cid):
    try:
        fp = sys.modules.get('josh_material_fpanel')
        if fp is None:
            fp = _load_local('josh_material_fpanel', 'Feature Panel.py')
            sys.modules['josh_material_fpanel'] = fp
        q = fp._qt_bind()
        if q is None:
            return
        mark = 'QTCommandDialogContentPanel' + cid
        for w in q.widgets():
            if mark in q.name(w):
                q.hide(w)
    except Exception:
        _log('hide failed:', traceback.format_exc().replace('\n', ' | '))


class _PickCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cmd = args.command
            _picking['cmd'] = cmd
            cmd.isAutoExecute = False
            cmd.isExecutedWhenPreEmpted = False
            try:
                cmd.isOKButtonVisible = False
                cmd.setDialogSize(1, 1)
                cmd.setDialogMinimumSize(1, 1)
            except Exception:
                pass
            si = cmd.commandInputs.addSelectionInput('pickBody', 'Body', 'Click a body')
            si.addSelectionFilter('Bodies')
            si.setSelectionLimits(1, 1)
            for evt, h in ((cmd.activate, _PickActivate()),
                           (cmd.inputChanged, _PickInput()),
                           (cmd.destroy, _PickDestroy())):
                evt.add(h)
                handlers.append(h)
        except Exception:
            _log('pick created failed:', traceback.format_exc().replace('\n', ' | '))


class _PickActivate(adsk.core.CommandEventHandler):
    def notify(self, args):
        _hide_palette(PICKING_ID)


class _PickInput(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            si = adsk.core.SelectionCommandInput.cast(args.input)
            if not si or si.selectionCount < 1:
                return
            a = _worn(si.selection(0).entity)
            if a is not None:
                set_last(a)
            app.fireCustomEvent(END_EVENT, '')
        except Exception:
            _log('pick input failed:', traceback.format_exc().replace('\n', ' | '))


class _PickDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        _picking['cmd'] = None


class _PickEnd(adsk.core.CustomEventHandler):
    def notify(self, args):
        cmd = _picking['cmd']
        if cmd is None:
            return
        try:
            cmd.doExecute(True)
        except Exception:
            _log('pick end failed:', traceback.format_exc().replace('\n', ' | '))


# ── Add-in ───────────────────────────────────────────────────────────────────

_EVENTS = []


def run(context):
    try:
        _read_last()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        hk = _hotkeys()
        for cid, name, desc in ((PICK_ID, PICK_NAME, PICK_DESC),
                                (APPLY_ID, APPLY_NAME, APPLY_DESC)):
            # Find-or-create, never deleteMe: the shortcut is bound to the definition.
            cd = ui.commandDefinitions.itemById(cid) or \
                ui.commandDefinitions.addButtonDefinition(cid, name, desc)
            h = _Created(cid)
            cd.commandCreated.add(h)
            handlers.append(h)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(cd)
            hk.declare(cid, name, desc, owner=OWNER, native=True)
        cd = ui.commandDefinitions.itemById(PICKING_ID) or \
            ui.commandDefinitions.addButtonDefinition(
                PICKING_ID, 'Pick Material (click)', 'The click Pick Material waits for.')
        h = _PickCreated()
        cd.commandCreated.add(h)
        handlers.append(h)

        try:
            app.unregisterCustomEvent(END_EVENT)
        except Exception:
            pass
        evt = app.registerCustomEvent(END_EVENT)
        for event, h in ((ui.commandStarting, _Starting()), (evt, _PickEnd())):
            event.add(h)
            handlers.append(h)
            _EVENTS.append((event, h))
        hk.attach()

        sys.modules[HOOK] = type(sys)(HOOK)
        sys.modules[HOOK].set_last = set_last
        _log('=== run(), last =', _last['name'] or '(none)')
    except Exception:
        ui.messageBox('Material failed to start:\n' + traceback.format_exc())


def stop(context):
    try:
        for event, h in _EVENTS:
            try:
                event.remove(h)
            except Exception:
                pass
        del _EVENTS[:]
        try:
            app.unregisterCustomEvent(END_EVENT)
        except Exception:
            pass
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid in (PICK_ID, APPLY_ID):
            ctrl = panel.controls.itemById(cid) if panel else None
            if ctrl:
                ctrl.deleteMe()
        handlers.clear()
        sys.modules.pop(HOOK, None)
        try:
            _hotkeys().withdraw(OWNER)
        except Exception:
            pass
        _log('=== stop()')
    except Exception:
        pass
