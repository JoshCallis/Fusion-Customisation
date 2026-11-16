"""
Auto-Project Origin
───────────────────
Every new sketch gets two things projected into it the moment it is created: its own
component's origin point, and the bounds rectangle it is being drawn inside.

Just the origin point — not the axes, not the planes.

THE BOUNDS RECTANGLE
────────────────────
Whichever of the component's three bounds sketches lies PARALLEL to the new one, as
construction geometry, along with the two midpoint points that are the only way to
dimension to the middle of a bound. Its OWN component's, never the root's: projecting
the model envelope into a sub-component would tie that component's geometry to the
wrong thing entirely. A component with no bounds sketches gets nothing, quietly —
that is every component predating them. See Bounds.py.

WHY THE COMPONENT'S OWN ORIGIN AND NOT THE ROOT'S
────────────────────────────────────────────────
This used to project the ROOT origin, on the reasoning that the root is where
the model actually starts.  That reasoning traded away containment: a sketch
holding a live reference to the root cannot be reused, copied into another
assembly, or have its component moved, without dragging the root along.

A component that only ever refers to its own origin is self-contained.  Move
it, copy it, nest it somewhere else — it still resolves.  That is worth more
than a link to the top of the tree, which is a reference the component does
not own.

Where every occurrence sits at the identity transform the two points land on
top of each other, so the swap is free; where an occurrence is transformed,
the component's own origin is the one that travels with the part, which is
the behaviour you want.

WHAT "PROJECT" MEANS WHEN THE PLANE MISSES THE ORIGIN
──────────────────────────────────────────────────
A point in a plane cannot be somewhere the plane does not reach.  If the
sketch plane does not pass through the component origin, the projected point
lands at the closest point on that plane, i.e. the origin dropped
perpendicularly onto the plane.  That is the normal meaning of a projection
and still tracks the origin, but it is not the origin itself.

HOW THE NEW SKETCH IS IDENTIFIED
────────────────────────────────
By diffing entity tokens across the SketchCreate command rather than reading
the timeline marker or design.activeEditObject.  The marker is only reliable
if nothing else has moved it, and activeEditObject raises
InternalValidationError for a sketch nested in an occurrence (the common case
here).  A token diff cannot be wrong about which sketch is new.
"""

import adsk.core, adsk.fusion, traceback, os, time, types

LOG = '/tmp/auto_project_origin.log'

# The per-component bounds record and its sketches. Loaded the way every add-in here
# loads a sibling module — a plain import cannot reach the AddIns root from inside a
# command folder. A failure here must never stop the origin projection, which worked
# long before bounds existed.
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None

CMD_SKETCH_CREATE = 'SketchCreate'

_app = None
_ui = None
_handlers = []
_before_tokens = None


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────────────

def _all_sketch_tokens(design):
    tokens = set()
    try:
        root = design.rootComponent
        for sk in root.sketches:
            try:
                tokens.add(sk.entityToken)
            except Exception:
                pass
        for occ in root.allOccurrences:
            for sk in occ.component.sketches:
                try:
                    tokens.add(sk.entityToken)
                except Exception:
                    pass
    except Exception:
        pass
    return tokens


def project_into_new_sketch(design, sketch):
    """Give a brand-new sketch its component origin and its bounds rectangle.

    The work itself is Bounds.project_new_sketch — a sketch made through the API fires
    no command event, so the plane box has to be able to do exactly this too, and one
    implementation of "what a new sketch gets" is the whole point. This add-in owns the
    TRIGGER; that module owns the act.
    """
    if bounds is None:
        return 'no bounds module'
    try:
        problems = bounds.project_new_sketch(design, sketch)
    except Exception as exc:
        return 'project failed: %s' % exc
    return '; '.join(problems[:3]) if problems else 'projected'


# ─────────────────────────────────────────────────────────────────────────────
# Triggers
# ─────────────────────────────────────────────────────────────────────────────

_pending_place = []      # sketch tokens waiting for the sketch edit to close


def _file_in_section(design, sketches):
    """Move new sketches into their component's section of the timeline. A native New
    Sketch is open for edit when SketchCreate ends (marker not at the end), so what
    cannot be placed now waits for SketchStop."""
    global _pending_place
    try:
        ph = bounds._phases() if bounds is not None else None
        if ph is None:
            return
        notes, done = ph.place_entities_in_section(design, sketches)
        if not done:
            _pending_place = [sk.entityToken for sk in sketches]
        for n in notes:
            _log('placement:', n)
    except Exception:
        _log('placement error:', traceback.format_exc().replace('\n', ' | '))


def _retry_pending(design):
    global _pending_place
    toks, _pending_place = _pending_place, []
    ents = []
    for t in toks:
        try:
            found = design.findEntityByToken(t)
            if found:
                ents.append(found[0])
        except Exception:
            pass
    if ents:
        _file_in_section(design, ents)


class _OnCommandStarting(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _before_tokens
        try:
            if self not in _handlers:
                return
            if args.commandId != CMD_SKETCH_CREATE:
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            _before_tokens = _all_sketch_tokens(design) if design else None
        except Exception:
            _log('commandStarting error:', traceback.format_exc().replace('\n', ' | '))


class _OnCommandTerminated(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _before_tokens
        try:
            if self not in _handlers:
                return
            if args.commandId == 'SketchStop' and _pending_place:
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if design:
                    _retry_pending(design)
                return
            if args.commandId != CMD_SKETCH_CREATE:
                return
            try:
                completed = (args.terminationReason ==
                             adsk.core.CommandTerminationReason.CompletedTerminationReason)
            except Exception:
                completed = True
            if not completed:
                _before_tokens = None
                return

            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                return
            before, _before_tokens = _before_tokens, None
            if before is None:
                return

            root = design.rootComponent
            new = []
            for sk in root.sketches:
                if sk.entityToken not in before:
                    new.append(sk)
            for occ in root.allOccurrences:
                for sk in occ.component.sketches:
                    if sk.entityToken not in before:
                        new.append(sk)
            if not new:
                _log('SketchCreate completed but no new sketch found')
                return
            for sk in new:
                outcome = project_into_new_sketch(design, sk)
                _log('SketchCreate -> "%s" in %s: %s' % (
                    sk.name, sk.parentComponent.name, outcome))
            _file_in_section(design, new)
        except Exception:
            _log('commandTerminated error:', traceback.format_exc().replace('\n', ' | '))


# ─────────────────────────────────────────────────────────────────────────────
# Add-in plumbing (reload/shortcut-safe pattern — no command definition here,
# so there is nothing to find-or-create; just add and remove the listeners)
# ─────────────────────────────────────────────────────────────────────────────

def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        starting = _OnCommandStarting()
        _ui.commandStarting.add(starting)
        _handlers.append(starting)

        terminated = _OnCommandTerminated()
        _ui.commandTerminated.add(terminated)
        _handlers.append(terminated)

        _log('=== run() — watching SketchCreate ===')
    except Exception:
        if _ui:
            _ui.messageBox('Auto-Project Origin run() failed:\n' + traceback.format_exc())


def stop(context):
    try:
        for h in list(_handlers):
            for evt in (_ui.commandStarting, _ui.commandTerminated):
                try:
                    evt.remove(h)
                except Exception:
                    pass
        _handlers.clear()
        _log('=== stop() ===')
    except Exception:
        pass
