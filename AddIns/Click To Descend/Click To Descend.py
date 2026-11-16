"""
Click To Descend
────────────────
Re-imagines the viewport click as a NAVIGATION gesture instead of a selection:

  • plain single-click on geometry that belongs to a *descendant* component
        → descend (activate) that component, leaving NOTHING selected
        (full-depth: jumps straight to the clicked geometry's parent component).
  • shift-click → left alone → native add-to-selection (no descend).
  • DRAG (window / box select, or dragging geometry) → left alone → native
        selection, however many entities it catches.
  • clicking geometry of the *active* component (or an ancestor) → left alone →
        native selection (so you can still pick faces/bodies/edges where you are).

Double-click is still being tuned (native Fusion double-click activates the
component, which collides with single-click-descend); for now this prototype
logs the full event sequence to /tmp/click_to_descend.log and cancels its own
pending descend whenever Fusion fires a native activate, so the two never stack.

Isolation + the folder-visibility policy are driven through fvs, identical to the
nav add-ins, so descend here behaves exactly like Go Down a Level reaching the
bottom in one step.
"""

import adsk.core, adsk.fusion, traceback
import os, time, ctypes, ctypes.util, threading
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load('fvs', 'Visibility Snapshot.py')
# Where the last physical click landed (canvas vs timeline / panel).
clickreg = _load('clickreg', 'Click Region.py')

_app = None
_ui = None
_handlers = []

DESCEND_EVENT_ID = 'ClickToDescend_Descend_001'
_descend_event = None

LOG = '/tmp/click_to_descend.log'

# ── double-click deference ────────────────────────────────────────────────────
# A double-click over the canvas (BTT turns a 3-finger tap into one) means GO TO
# TOP, and its FIRST click selects geometry just like a real single click — so a
# descend must not fire until we're sure no second click is coming. The descend is
# therefore DEFERRED by DESCEND_DELAY_S; when it finally runs it bails out if
#   • a fresh double-click stamp is pending (the Browser add-in owns it), or
#   • an up-navigation command (Go to Top / Go Up) started since the click —
#     covering the race where the Browser consumed the stamp first.
DBLCLICK_FILE   = '/tmp/fusion_dblclick'
CLICK_CMD_FILE_STAMP = '/tmp/fusion_click_cmd'   # event tap stamps every real left-down
# How long a descend is deferred so a double-click can cancel it. Measured 2026-08-18
# with btt_click_timing.py over 24 clicks / 8 double-clicks: the event tap writes the
# click and the double-click stamp from the SAME CGEvent (clickState==2), so the gap is
# 0 ms every time — BTT's synthetic pair lands ~2 ms apart, not "well inside 220 ms".
# Same run showed clicks 151/165 ms apart do NOT register as a double, so the useful
# window is bounded well under that. 0.08 leaves ~40x margin over the observed gap and
# takes 140 ms off click-to-update latency. Raise it if a MANUAL (non-triple-tap)
# double-click ever descends before it is recognised.
DESCEND_DELAY_S = 0.08
_pending_timer  = None          # threading.Timer for the deferred descend
_pending_ms     = 0.0           # epoch seconds when the descend was scheduled
_last_upnav_ms  = 0.0           # epoch seconds a nav command last STARTED
# Every nav command that keeps the selection by RE-ADDING it after activate()
# clears it. Their re-adds fire the same selection events as a click, and the
# re-add can land while the user's real click (the one made BEFORE pressing the
# nav hotkey) is still fresh — so freshness alone isn't enough: a descend-worthy
# click must be NEWER than the last nav-command start. The start time is also
# stamped to UPNAV_STAMP so the Browser add-in can apply the same rule.
UP_NAV_CMDS = ('UnisolateTopLevelCmd', 'GoToParentComponentCmd',
               'ActivateAndIsolateCmd', 'GoToBottomLevelCmd')
UPNAV_STAMP = '/tmp/fusion_upnav'


def _cancel_pending():
    global _pending_timer
    t = _pending_timer
    _pending_timer = None
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass


def _click_stamp_ms():
    """Epoch ms of the last real left-down over Fusion (event-tap stamp); 0 when
    absent so 'no stamp' reads as 'no click'."""
    try:
        with open(CLICK_CMD_FILE_STAMP) as f:
            return int(f.read().split()[0])
    except Exception:
        return 0


def _click_is_descend_worthy(max_age_ms=600):
    """The last real click is fresh, NOT a drag, AND newer than the last nav-command
    start — i.e. it can't be a rubber-band select, nor the click that preceded a
    Go-to-Top/Go-Up/Go-Down whose selection re-add we're now seeing."""
    ms = _click_stamp_ms()
    if not (0 <= time.time() * 1000 - ms <= max_age_ms):
        return False
    if clickreg.click_was_drag():
        return False
    return ms > _last_upnav_ms * 1000


def _dblclick_pending(max_age_ms=900):
    """A fresh double-click stamp exists. PEEK ONLY — never delete the file; the
    Browser add-in consumes it to fire Go to Top."""
    try:
        with open(DBLCLICK_FILE) as f:
            ms = int(f.read().strip())
        return 0 <= (time.time() * 1000 - ms) <= max_age_ms
    except Exception:
        return False


def _schedule_descend(token):
    """Replace any pending descend with a fresh deferred one. The timer thread only
    calls fireCustomEvent (thread-safe by design); all Fusion API work stays on the
    main thread in _do_descend."""
    global _pending_timer, _pending_ms
    _cancel_pending()
    _pending_ms = time.time()
    t = threading.Timer(DESCEND_DELAY_S,
                        lambda: _app.fireCustomEvent(DESCEND_EVENT_ID, token))
    t.daemon = True
    _pending_timer = t
    t.start()

# ── shift detection (CoreGraphics, same as the old Select add-in) ──────────────
_cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics'))
_cg.CGEventSourceKeyState.restype = ctypes.c_bool
_cg.CGEventSourceKeyState.argtypes = [ctypes.c_int, ctypes.c_uint16]
_HID = 1
_VK_SHIFT, _VK_RSHIFT = 56, 60
_VK_CMD,   _VK_RCMD   = 55, 54
SHIFT_SUPPRESS_FILE = '/tmp/fusion_shift_suppress'

def _shift_down():
    try:
        if os.path.exists(SHIFT_SUPPRESS_FILE):
            return False
        return bool(_cg.CGEventSourceKeyState(_HID, _VK_SHIFT) or
                    _cg.CGEventSourceKeyState(_HID, _VK_RSHIFT))
    except Exception:
        return False

def _cmd_down():
    try:
        return bool(_cg.CGEventSourceKeyState(_HID, _VK_CMD) or
                    _cg.CGEventSourceKeyState(_HID, _VK_RCMD))
    except Exception:
        return False

def _modifier_down():
    """Any modifier that means 'select, don't descend':
       shift = add to selection, cmd = select just this one."""
    return _shift_down() or _cmd_down()


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write(f"{time.time():.3f}  " + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ── occurrence helpers ────────────────────────────────────────────────────────
def _owning_occurrence(entity):
    """Immediate parent-component instance that owns the geometry (deepest)."""
    try:
        if isinstance(entity, adsk.fusion.Occurrence):
            return entity
        if getattr(entity, 'assemblyContext', None):
            return entity.assemblyContext
        body = getattr(entity, 'body', None)
        if body is not None and getattr(body, 'assemblyContext', None):
            return body.assemblyContext
        sk = getattr(entity, 'parentSketch', None)
        if sk is not None and getattr(sk, 'assemblyContext', None):
            return sk.assemblyContext
    except Exception:
        pass
    return None


def _active_occ(design):
    """Where a click descends FROM: the active occurrence, or the view in suspend
    mode (fvs.view_occ — there a descend is a peek)."""
    if fvs.is_suspended():
        return fvs.view_occ(design)
    a = fvs.recall_active_occ(design)
    if not (a and a.component == design.activeComponent):
        a = fvs.find_occurrence(design, design.activeComponent)
    return a


def _strict_descendant(design, target, active=None):
    """True only when `target` sits strictly BELOW the active occurrence — the one
    case where a click means 'descend'. Already being in target (or below it), or
    target being on another branch, means the click is a plain selection."""
    tpath = fvs._occ_path_str(target)
    if not tpath:
        return False
    if active is None:
        active = _active_occ(design)
    apath = fvs._occ_path_str(active) if active else ''
    if apath == '':
        return True                         # at root → any occurrence is deeper
    return tpath != apath and tpath.startswith(apath + '/')


def _descend_target(design, entity):
    """Return the occurrence to descend into, or None if the click should be left
    to native selection (geometry of the active component or an ancestor)."""
    target = _owning_occurrence(entity)
    if target is None:
        return None
    return target if _strict_descendant(design, target) else None


# ── descend (main thread, via custom event) ───────────────────────────────────
def _do_descend(token):
    try:
        # Asked again here as well as at scheduling time: the drag verdict is stamped
        # on mouse-UP, so a click that Fusion selected on mouse-DOWN and the user then
        # dragged only reveals itself as a drag inside the defer window.
        if clickreg.click_was_drag():
            _log('DESCEND SKIP: the click turned into a drag')
            return
        if _dblclick_pending():
            _log('DESCEND SKIP: double-click stamp pending (Go to Top owns this click)')
            return
        if _last_upnav_ms >= _pending_ms - 0.05:
            _log('DESCEND SKIP: up-nav command started since the click')
            return
        design = adsk.fusion.Design.cast(_app.activeProduct)
        if not design:
            return
        if fvs.master_session_live(design):
            # Descending activates a component, which would leave a master opened by
            # Edit Original (or break the hop into it) — see fvs's master session.
            _log('DESCEND SKIP: master session')
            return
        ents = design.findEntityByToken(token)
        target = ents[0] if ents else None
        if not isinstance(target, adsk.fusion.Occurrence):
            return
        active = _active_occ(design)
        # RE-VALIDATE against the active component as it is NOW, not as it was when
        # the click landed. Inside our defer window the Browser add-in's own
        # jump-down (~350ms after the click) can already have activated the target
        # and re-added the user's selection. Descending again would re-isolate,
        # re-snapshot, and clear that selection — the "I'm already in this component
        # and it deselects me" bug. Same guard covers manual activation, an undo, or
        # any other activate that beats the timer.
        if not _strict_descendant(design, target, active):
            _log('DESCEND SKIP: active moved to', active.name if active else 'root',
                 '- target', target.name, 'is no longer below it')
            return
        _log('DESCEND ->', target.name, 'from', active.name if active else 'root')
        if fvs.is_suspended():
            fvs.peek(design, target)          # fvs § Suspend mode: peek
            return
        fvs.take_snapshot(design, active)
        fvs.isolate_occurrence_no_snapshot(design, target)
        _ui.activeSelections.clear()
        target.activate()
        fvs.remember_activation(target)
        fvs.apply_policy(design)
        _ui.activeSelections.clear()
    except Exception:
        _log('DESCEND ERR', traceback.format_exc())


# ── handlers ──────────────────────────────────────────────────────────────────
class _SelChanged(adsk.core.ActiveSelectionEventHandler):
    def notify(self, args):
        try:
            sel = _ui.activeSelections
            n = sel.count
            mod = _modifier_down()
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if n > 1:
                # A multi-entity selection arrives one event per entity, so the FIRST
                # one already looked like a plain pick and scheduled a descend. Kill it:
                # picking several things at once is a selection, never a navigation.
                _cancel_pending()
            if not design or n == 0:
                return
            # Only a plain single-entity pick backed by a FRESH physical click is a
            # descend gesture. A box / drag select (n > 1), a held modifier (shift =
            # add, cmd = select one), or a selection with no recent left-down (a
            # PROGRAMMATIC re-add — Go to Top / Go Up keep the selection by re-adding
            # it after activate() clears it) is never a reason to descend; reacting
            # to re-adds yanked the user back down right after a hotkey Go-to-Top.
            ent = sel.item(0).entity
            fresh = _click_is_descend_worthy()
            # A click on the TIMELINE selects the feature's geometry and fires this
            # very event, but it is a scrub, not a pick — descending on it dragged
            # the active component around behind the timeline. Same for any other
            # Fusion widget that can drive the selection; only the 3D canvas is a
            # navigation gesture.
            # (Asked only once the cheap tests pass — the Qt read is memoised
            # against the click stamp, and a stale click must not claim the memo.)
            where, canvas = '', True
            if fresh and not mod and n == 1:
                where = clickreg.click_widget_class()
                canvas = clickreg.click_over_canvas(where)
            tgt = (None if (mod or n != 1 or not fresh or not canvas)
                   else _descend_target(design, ent))
            _log(f'SEL n={n} mod={mod} fresh={fresh} type={type(ent).__name__}',
                 'where=' + (where or '-'),
                 'active=' + design.activeComponent.name,
                 'descend=' + (tgt.name if tgt else 'NO'))
            if tgt is None:
                return                       # leave native selection alone
            # Deferred so a double-click's second click can veto it — see the
            # double-click deference block up top.
            _schedule_descend(tgt.entityToken)
        except Exception:
            _log('SEL ERR', traceback.format_exc())


class _CmdStart(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _last_upnav_ms
        cid = args.commandId
        if cid == 'SelectCommand':
            return
        if cid == 'UndoCommand':
            _log('UNDO STACK:\n' + ''.join(traceback.format_stack()))
        if cid in UP_NAV_CMDS:
            _last_upnav_ms = time.time()
            try:
                with open(UPNAV_STAMP, 'w') as f:
                    f.write(str(int(_last_upnav_ms * 1000)))
            except Exception:
                pass
        _log('  cmdStart', cid)


class _Descend(adsk.core.CustomEventHandler):
    def notify(self, args):
        with fvs.one_step('Navigate'):   # the whole descend is one undo step
            _do_descend(args.additionalInfo or '')


# ── lifecycle ─────────────────────────────────────────────────────────────────
def run(context):
    global _app, _ui, _descend_event
    # RETIRED 2026-09-26: the Click Actions add-in owns every viewport click now
    # (fusion_ui/clicks.json, Control Room ▸ Fusion ▸ Clicks) — plain click on a body is
    # a rule there, not this add-in's hard-wired full descend. Running both would act
    # twice on one click, so this stays inert even if started. Kept for its history.
    return
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        open(LOG, 'a').write(f"{time.time():.3f}  === Click To Descend run() ===\n")
        try:
            _descend_event = _app.registerCustomEvent(DESCEND_EVENT_ID)
        except Exception:
            _descend_event = _app.customEvents.itemById(DESCEND_EVENT_ID)
        dh = _Descend(); _descend_event.add(dh); _handlers.append(dh)

        sh = _SelChanged(); _ui.activeSelectionChanged.add(sh); _handlers.append(sh)
        ch = _CmdStart();  _ui.commandStarting.add(ch);        _handlers.append(ch)
    except Exception:
        if _ui:
            _ui.messageBox('Click To Descend run() failed:\n' + traceback.format_exc())


def stop(context):
    try:
        _cancel_pending()
        for h in _handlers:
            for evt in (_ui.activeSelectionChanged, _ui.commandStarting):
                try:
                    evt.remove(h)
                except Exception:
                    pass
            if _descend_event:
                try:
                    _descend_event.remove(h)
                except Exception:
                    pass
        try:
            _app.unregisterCustomEvent(DESCEND_EVENT_ID)
        except Exception:
            pass
        _handlers.clear()
    except Exception:
        pass
