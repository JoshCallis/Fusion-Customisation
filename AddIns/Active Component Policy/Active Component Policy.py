"""
Active Component Policy
───────────────────────
Always-on watcher that keeps the body / sketch / origin folder visibility in
sync with the global toggles every time the active component changes.

It covers the cases the navigation add-ins can't reach on their own:
  • manual activation from the browser  (FusionActivateLocalCompCmd)
  • new-component creation               (FusionCreateNewComponentCommand,
                                          WrapInNewComponentCmd / Make Component)
  • opening a document                   (documentOpened)

All the actual logic lives in fvs.apply_policy() — this add-in is just the
trigger.  The navigation add-ins also call apply_policy() directly so the
update lands synchronously in their own event batch (no flash); the watcher is
the backstop for everything else.  apply_policy is idempotent, so double-firing
is harmless.
"""

import adsk.core, adsk.fusion, traceback, os
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load('fvs', 'Visibility Snapshot.py')

# Commands after which the active component may have changed → just re-apply the
# folder policy (our own Browser / nav add-ins already handle their own isolation).
ACTIVATION_CMDS = {
    'FusionActivateLocalCompCmd',       # activate component (browser dbl-click / our nav)
}

# Creating a new component should behave like NAVIGATING INTO it: isolate the new
# active component (+ snapshot the parent) exactly as Go Down does, not merely
# re-run the folder policy.
CREATION_CMDS = {
    'FusionCreateNewComponentCommand',  # native New Component
    'WrapInNewComponentCmd',            # custom Make Component add-in
}

# Sketch enter/exit is NOT handled here: Go Down's SketchActivate/SketchStop
# listener is global (it fires for every entry and exit, however started) and
# already restores the per-sketch bulbs and re-applies the policy. A second pass
# here only doubled the exit cost — and ran first, before Go Down stamps the exit,
# so it could still read the sketch as open.

_app = None
_ui = None
_handlers = []
_term_handler = None
_doc_handler = None


def _apply():
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        if design:
            fvs.apply_policy(design)
    except Exception:
        pass


def _isolate_active():
    """Treat a freshly-created component like navigating into it: snapshot the
    parent level, isolate onto the new active occurrence, remember it, then apply
    the folder policy.  Mirrors Go Down's _activate_level."""
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        if not design:
            return
        new_occ = fvs._active_occ(design)
        if new_occ is not None:
            parent = None
            try:
                parent = new_occ.assemblyContext
            except Exception:
                parent = None
            fvs.take_snapshot(design, parent)
            if fvs.isolate_is_active():
                fvs.isolate_occurrence_no_snapshot(design, new_occ)
            fvs.remember_activation(new_occ)
        fvs.apply_policy(design)
    except Exception:
        pass


class _TermHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if self not in _handlers:
                return
            # Filter BEFORE opening the step: one_step costs four text commands
            # (Transaction List/Start/List/Abort), and this fires after every command.
            if args.commandId not in CREATION_CMDS and args.commandId not in ACTIVATION_CMDS:
                return
            with fvs.one_step('Visibility'):   # everything below is one undo step
                self._dispatch(args)
        except Exception:
            pass

    def _dispatch(self, args):
        if args.commandId in CREATION_CMDS:
            _isolate_active()
        elif args.commandId in ACTIVATION_CMDS:
            _apply()


class _DocHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        _apply()


def run(context):
    global _app, _ui, _term_handler, _doc_handler
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        _term_handler = _TermHandler()
        _ui.commandTerminated.add(_term_handler)
        _handlers.append(_term_handler)

        _doc_handler = _DocHandler()
        _app.documentOpened.add(_doc_handler)

        _apply()  # bring current document in line immediately
    except Exception:
        if _ui:
            _ui.messageBox('Active Component Policy run() failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _term_handler, _doc_handler
    try:
        if _term_handler:
            _ui.commandTerminated.remove(_term_handler)
            _term_handler = None
        if _doc_handler:
            _app.documentOpened.remove(_doc_handler)
            _doc_handler = None
        _handlers.clear()
    except Exception:
        pass
