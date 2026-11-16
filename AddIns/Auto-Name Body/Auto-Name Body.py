"""
Auto-Name Body — add-in.

When a new body is created it keeps Fusion's default name ("Body1", "Body2", …).
This renames such default-named bodies to their parent component's name. If a body
with that name already exists in the component, a number is appended ("Plate",
"Plate2", "Plate3", …).

Fusion has no "body created" event, so we run the scan after every command finishes
(ui.commandTerminated) — the standard hook for "react to a model change". The scan
only ever touches bodies still carrying a default name, so re-running it is cheap
and idempotent (a body we already renamed no longer matches and is left alone).
Renaming via the API fires no command event, so this can't recurse.
"""

import adsk.core, adsk.fusion
import re, traceback

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []
_running = False

# Fusion's default body names: "Body1", "Body 12", etc.
DEFAULT_BODY_RE = re.compile(r'^Body\s*\d+$')


def _unique_body_name(base, taken):
    """`base` if free in `taken`, else `base2`, `base3`, … (first that's free)."""
    if base not in taken:
        return base
    i = 2
    while '{}{}'.format(base, i) in taken:
        i += 1
    return '{}{}'.format(base, i)


def _rename_default_bodies(design):
    # allComponents covers the root and every sub-component definition. Bodies
    # belong to the component definition, so renaming here names the body after
    # the component it lives in (shared across all of that component's instances).
    for comp in design.allComponents:
        try:
            bodies = comp.bRepBodies
            if bodies.count == 0:
                continue
            base = comp.name
            for b in bodies:
                if not DEFAULT_BODY_RE.match(b.name):
                    continue
                taken = set(x.name for x in bodies)
                taken.discard(b.name)             # the body keeps its own slot free
                new_name = _unique_body_name(base, taken)
                if new_name != b.name:
                    b.name = new_name
        except Exception:
            # A component we can't read/rename (e.g. external/read-only) — skip it.
            continue


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if not _running:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                _rename_default_bodies(design)
        except Exception:
            pass


def run(context):
    global _running
    try:
        _running = True
        h = CommandTerminatedHandler()
        ui.commandTerminated.add(h)
        handlers.append(h)
        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Auto-Name Body failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _running
    try:
        _running = False
        for h in handlers:
            try:
                ui.commandTerminated.remove(h)
            except Exception:
                pass
        handlers.clear()
    except Exception:
        pass
