"""
Click Actions
─────────────
What a click in the viewport DOES, as data: ~/Code/Scripts/fusion_ui/clicks.json
(edited on Control Room ▸ Fusion ▸ Clicks) binds

    gesture  +  held modifiers  +  what was clicked   →   a Fusion command id

  gesture   click  a stationary left click on the canvas (a drag / box select never)
            tap2   a two-finger tap (right-click) — only while no command runs
            tap3   a three-finger touch (reanchor, no pick → 'any'), or BetterTouchTool's
                   synthetic double-click if that is ever turned back on
  mods      exactly these held: space, shift, cmd, alt, ctrl
  on        body · sketch · component · construction · empty · any
  do        a command id ('' = just select, Fusion's own behaviour)

A rule that names the object beats an 'any' rule; otherwise the first one wins.

THE CLICK PATH IS CLICK TO DESCEND'S, UNCHANGED — only the decision of what to do
is new. It is driven by Fusion's own selection event (so it sees the pick the moment
Fusion makes it), gated exactly as before (fresh physical click, not a drag, on the
canvas, newer than the last nav command), deferred DESCEND_DELAY_S so a three-finger
tap's second click can veto it, and then:

  • Go to Bottom Level  → Click To Descend's full descend, done directly: snapshot,
                          isolate the pick's component, activate it, apply the
                          folder policy, leave nothing selected.
  • Go Down a Level     → the same, one component toward the pick (the next one
                          down its path that the browser shows).
  Both only when the pick lies strictly BELOW the active component, never in a
  master session, and never open a sketch.
  • any other command   → run as a command on the selection (cd.execute()).

A click that will go down has its pick cleared the moment it's judged one, so the
body never flashes selected on the way down; if the go-down is called off (drag,
three-finger tap, nav key, a second pick from the same click) the pick is put back.

Why not drive clicks off the event tap's gesture file, like the taps: it reports on
mouse-UP, and ~10 ms later Fusion often hasn't made its pick yet — the click read an
empty selection and just selected. Running the real Go Down command instead of
activating directly added another 50–200 ms. Both were tried (2026-09-26) and felt
worse than this.

Modifiers are read the way Click To Descend read shift/cmd — the HID key state at
the selection event. Held space is Karabiner's F20 report (keycode 90); in Fusion
that report also holds Option, so Option under space is the space itself.

The taps, and a click that picked nothing, come from the FusionScrollFilter event
tap instead (CLICK_EVENT_FILE — a two-finger tap never reaches Fusion, and an empty
click fires no selection event). A poller thread watches that file and hands each
gesture to the main thread; nothing touches the Fusion API off the main thread.

Replaces Click To Descend (inert, kept for history) and the Browser add-in's
hard-wired jump-down, right-click → Go Up and double-click → Go to Top.
Log: /tmp/click_actions.log.
"""

import adsk.core, adsk.fusion, traceback
import ctypes, ctypes.util, json, os, sys, threading, time
import types as _types

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m
fvs = _load('fvs', 'Visibility Snapshot.py')
# Where the last physical click landed (canvas vs timeline / panel), and drags.
clickreg = _load('clickreg', 'Click Region.py')

RULES_FILE       = '/Users/josh/Code/Scripts/fusion_ui/clicks.json'
CLICK_EVENT_FILE = '/tmp/fusion_click_event'   # in: "<down_ms> <gesture> <mods> <synthetic>" (FusionScrollFilter)
CLICK_CMD_FILE_STAMP = '/tmp/fusion_click_cmd' # in: the event tap stamps every real left-down
DBLCLICK_FILE    = '/tmp/fusion_dblclick'      # in: the event tap stamps a three-finger tap's pair
LOG              = '/tmp/click_actions.log'
CLICK_EVENT_ID   = 'ClickActions_Click_001'    # a deferred click's action, on the main thread
GESTURE_EVENT_ID = 'ClickActions_Gesture_001'  # a tap / empty click from the event tap

# How long a click's action is deferred so a three-finger tap (BTT's two synthetic
# clicks, 0–2 ms apart) can veto it. Click To Descend's measured value, kept as is.
DESCEND_DELAY_S = 0.08
POLL_S          = 0.005   # the gesture-file poller (one stat())
STALE_MS        = 1000    # a gesture older than this when first seen is not acted on
EMPTY_WAIT_S    = 0.15    # an empty click waits this long for Fusion to have cleared the pick

DOWN_CMD   = 'ActivateAndIsolateCmd'
BOTTOM_CMD = 'GoToBottomLevelCmd'
TOP_CMD    = 'UnisolateTopLevelCmd'
# Every nav command that keeps the selection by RE-ADDING it after activate() clears
# it. Their re-adds fire the same selection events as a click, and can land while the
# user's real click (made BEFORE pressing the nav hotkey) is still fresh — so a click
# worth acting on must be NEWER than the last nav-command start.
UP_NAV_CMDS = ('UnisolateTopLevelCmd', 'GoToParentComponentCmd',
               'ActivateAndIsolateCmd', 'GoToBottomLevelCmd')

_app = None
_ui = None
_handlers = []
_events = {}
_stop = threading.Event()
_pending_timer = None     # threading.Timer for the deferred click action
_pending_ms = 0.0         # epoch seconds when it was scheduled
_last_upnav_ms = 0.0      # epoch seconds a nav command last STARTED
_last_sel_click = 0       # click stamp the selection path last saw (empty clicks defer to it)
_rules_cache = (None, [])
_hidden = None            # the pick a pending go-down took off screen (see _hide)
_hidden_click = 0         # the click stamp it came from
_hiding = False           # our own clear/re-add is firing selection events


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write(f"{time.time():.3f}  " + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ── rules ─────────────────────────────────────────────────────────────────────
def _rules():
    """clicks.json's rules, re-read when its mtime changes. A file that fails to
    parse keeps the last good rules, so a half-written save never blanks them."""
    global _rules_cache
    try:
        mt = os.path.getmtime(RULES_FILE)
    except OSError:
        return _rules_cache[1]
    if mt != _rules_cache[0]:
        try:
            with open(RULES_FILE) as f:
                rules = json.load(f).get('rules') or []
            _rules_cache = (mt, [r for r in rules if isinstance(r, dict)])
        except Exception:
            _log('RULES unreadable, keeping last good:', traceback.format_exc().splitlines()[-1])
    return _rules_cache[1]


def _match(gesture, mods, kind):
    """The command for this gesture: exact gesture and modifier set, the object named
    beats 'any', file order breaks ties. '' when nothing matches (just select)."""
    want = frozenset(mods)
    best = None
    for r in _rules():
        if r.get('gesture') != gesture or frozenset(r.get('mods') or []) != want:
            continue
        on = r.get('on') or 'any'
        if on == kind:
            return r.get('do') or ''
        if on == 'any' and best is None:
            best = r.get('do') or ''
    return best or ''


# ── held keys (CoreGraphics HID state, as Click To Descend read shift/cmd) ─────
_cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics'))
_cg.CGEventSourceKeyState.restype = ctypes.c_bool
_cg.CGEventSourceKeyState.argtypes = [ctypes.c_int, ctypes.c_uint16]
_HID = 1
_KEYS = {'shift': (56, 60), 'cmd': (55, 54), 'alt': (58, 61), 'ctrl': (59, 62)}
_VK_F20 = 90                                  # Karabiner's held-space report
SHIFT_SUPPRESS_FILE = '/tmp/fusion_shift_suppress'


def _down(*codes):
    try:
        return any(_cg.CGEventSourceKeyState(_HID, c) for c in codes)
    except Exception:
        return False


def _held():
    """The modifiers held right now, spelled as clicks.json spells them."""
    space = _down(_VK_F20)
    out = ['space'] if space else []
    for name, codes in _KEYS.items():
        if name == 'alt' and space:
            continue                          # Fusion's held space IS Option
        if name == 'shift' and os.path.exists(SHIFT_SUPPRESS_FILE):
            continue
        if _down(*codes):
            out.append(name)
    return out


# ── what was clicked ──────────────────────────────────────────────────────────
def _kind(ent):
    if ent is None:
        return 'empty'
    if isinstance(ent, adsk.fusion.Occurrence):
        return 'component'
    if isinstance(ent, (adsk.fusion.BRepBody, adsk.fusion.BRepFace,
                        adsk.fusion.BRepEdge, adsk.fusion.BRepVertex)):
        return 'body'
    if isinstance(ent, adsk.fusion.Sketch) or getattr(ent, 'parentSketch', None) is not None:
        return 'sketch'
    if isinstance(ent, (adsk.fusion.ConstructionPlane, adsk.fusion.ConstructionAxis,
                        adsk.fusion.ConstructionPoint)):
        return 'construction'
    return 'other'


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
    """Where a click navigates FROM: the active occurrence — or, in suspend mode, the
    view (fvs.view_occ), since every move there is a peek."""
    if fvs.is_suspended():
        return fvs.view_occ(design)
    a = fvs.recall_active_occ(design)
    if not (a and a.component == design.activeComponent):
        a = fvs.find_occurrence(design, design.activeComponent)
    return a


def _strict_descendant(design, target, active=None):
    """True only when `target` sits strictly BELOW the active occurrence — the one
    case where a click means 'go down'. Already being in target (or below it), or
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


def _one_level_toward(design, target, active):
    """The next occurrence down target's path from the active one, stepping over
    pass-through wrappers exactly as Go Down a Level does."""
    _, path = fvs._find_occ_info(design, target)
    if not path:
        return target
    apath = fvs._occ_path_str(active) if active else ''
    k = -1
    for i, occ in enumerate(path):
        p = fvs._occ_path_str(occ)
        if apath and (apath == p or apath.startswith(p + '/')):
            k = i
    nxt = k + 1
    while nxt < len(path) - 1 and fvs.is_disregarded(path[nxt]):
        nxt += 1
    return path[nxt] if nxt < len(path) else target


# ── the click path (Click To Descend's) ───────────────────────────────────────
def _cancel_pending():
    global _pending_timer
    t, _pending_timer = _pending_timer, None
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


def _click_is_fresh(max_age_ms=600):
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
    """A fresh three-finger-tap stamp exists. PEEK ONLY — never delete the file."""
    try:
        with open(DBLCLICK_FILE) as f:
            ms = int(f.read().strip())
        return 0 <= (time.time() * 1000 - ms) <= max_age_ms
    except Exception:
        return False


def _schedule(cid, token):
    """Replace any pending click action with a fresh deferred one. The timer thread
    only calls fireCustomEvent (thread-safe by design); all Fusion API work stays on
    the main thread in _do_click."""
    global _pending_timer, _pending_ms, _hidden
    _cancel_pending()
    _hidden = None            # a new click supersedes the last one's hidden pick
    _pending_ms = time.time()
    payload = json.dumps({'cid': cid, 'tok': token})
    t = threading.Timer(DESCEND_DELAY_S,
                        lambda: _app.fireCustomEvent(CLICK_EVENT_ID, payload))
    t.daemon = True
    _pending_timer = t
    t.start()


# ── the pick a go-down doesn't want seen ──────────────────────────────────────
# A plain click that goes down is navigation, not selection, but Fusion has already
# picked (and highlighted) the body by the time we hear of it — and that highlight
# stayed up through the defer window and the activate. So the pick is cleared the
# moment the click is judged a go-down, and put back on every path where the
# go-down is called off (a drag, a three-finger tap, a nav key, a second pick), so a
# click that ends up NOT navigating still selects exactly as Fusion would have.
def _hide(ent, click_ms):
    global _hidden, _hidden_click, _hiding
    _hiding = True
    try:
        _ui.activeSelections.clear()
        _hidden, _hidden_click = ent, click_ms
    except Exception:
        _log('HIDE ERR', traceback.format_exc().splitlines()[-1])
    finally:
        _hiding = False


def _unhide():
    """Put the hidden pick back (unless it's already selected or gone)."""
    global _hidden, _hiding
    ent, _hidden = _hidden, None
    if ent is None:
        return
    _hiding = True
    try:
        if not ent.isValid:
            return
        sel = _ui.activeSelections
        if any(sel.item(i).entity == ent for i in range(sel.count)):
            return
        sel.add(ent)
        _log('  pick restored')
    except Exception:
        _log('UNHIDE ERR', traceback.format_exc().splitlines()[-1])
    finally:
        _hiding = False


def _abandon():
    """The pending go-down is off: cancel it and give the click its pick back."""
    _cancel_pending()
    _unhide()


def _go(design, active, occ):
    """Click To Descend's descend, onto `occ`: snapshot where we are, isolate and
    activate it, apply the folder policy, leave nothing selected. In suspend mode a
    peek instead (fvs § Suspend mode: peek): no activation, no snapshot."""
    if fvs.is_suspended():
        fvs.peek(design, occ)
        _ui.activeSelections.clear()
        return
    fvs.take_snapshot(design, active)
    fvs.isolate_occurrence_no_snapshot(design, occ)
    _ui.activeSelections.clear()
    occ.activate()
    fvs.remember_activation(occ)
    fvs.apply_policy(design)
    _ui.activeSelections.clear()


def _do_click(cid, token):
    global _hidden
    try:
        if _do_click_inner(cid, token):
            _hidden = None          # went down: the pick was never meant to show
    finally:
        _unhide()                   # any SKIP: the click keeps its pick after all


def _do_click_inner(cid, token):
    """True when it went down."""
    # Asked again here as well as at scheduling time: the drag verdict is stamped on
    # mouse-UP, so a click that Fusion selected on mouse-DOWN and the user then
    # dragged only reveals itself as a drag inside the defer window.
    if clickreg.click_was_drag():
        _log('SKIP: the click turned into a drag')
        return
    if _dblclick_pending():
        _log('SKIP: three-finger tap pending (it owns this click)')
        return
    if _last_upnav_ms >= _pending_ms - 0.05:
        _log('SKIP: nav command started since the click')
        return
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        return
    if cid not in (DOWN_CMD, BOTTOM_CMD):
        cd = _ui.commandDefinitions.itemById(cid)
        if cd is None:
            _log('SKIP: no such command', cid)
            return
        cd.execute()
        return
    if fvs.master_session_live(design):
        # Activating a component would leave a master opened by Edit Original (or
        # break the hop into it) — see fvs's master session.
        _log('SKIP: master session')
        return
    ents = design.findEntityByToken(token) if token else []
    target = ents[0] if ents else None
    if not isinstance(target, adsk.fusion.Occurrence):
        return
    active = _active_occ(design)
    # RE-VALIDATE against the active component as it is NOW: an activation (manual,
    # undo, a key) can beat the timer, and going down again would re-isolate,
    # re-snapshot and clear a selection you already have.
    if not _strict_descendant(design, target, active):
        _log('SKIP: active moved to', active.name if active else 'root')
        return
    occ = target if cid == BOTTOM_CMD else _one_level_toward(design, target, active)
    _log('  ->', occ.name, 'from', active.name if active else 'root')
    _go(design, active, occ)
    return True


class _SelChanged(adsk.core.ActiveSelectionEventHandler):
    def notify(self, args):
        global _last_sel_click, _hidden
        if _hiding:
            return                              # our own clear / re-add
        try:
            # Our Extrude runs with its palette hidden, so activeCommand does not show
            # it — but a body clicked under it is being added to the cut, not asking
            # to be activated.
            hook = sys.modules.get('fusion_extrude_hook')
            if hook is not None and getattr(hook, 'running', lambda: False)():
                _cancel_pending()
                _hidden = None                  # never re-add a body into the cut
                return
            # Suspend mode keeps a running command's picks its own: a click there is
            # input to the command, never a peek (and never hidden from it).
            if fvs.is_suspended() and _ui.activeCommand != 'SelectCommand':
                return
            sel = _ui.activeSelections
            n = sel.count
            mods = _held()
            if (_hidden is not None and n and _pending_timer is not None
                    and _click_stamp_ms() == _hidden_click):
                if n == 1 and sel.item(0).entity == _hidden:
                    _hide(_hidden, _hidden_click)   # Fusion re-picked it: keep it hidden
                    return
                # More picks from the SAME click (a window select sweeping on from
                # the body it started on): a selection after all, never a go-down.
                _abandon()
                return
            if n > 1 and not mods:
                # A multi-entity selection arrives one event per entity, so the FIRST
                # one already looked like a plain pick and scheduled an action. Kill
                # it: picking several things at once is a selection, never a click.
                _abandon()
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design or n == 0 or (n > 1 and not mods):
                return
            # Only a pick backed by a FRESH physical click on the CANVAS acts. A
            # selection with no recent left-down is a PROGRAMMATIC re-add (Go to Top /
            # Go Up keep the selection by re-adding it); a click on the TIMELINE selects
            # the feature's geometry and fires this very event, but it is a scrub.
            if not _click_is_fresh():
                return
            where = clickreg.click_widget_class()
            if not clickreg.click_over_canvas(where):
                return
            _last_sel_click = _click_stamp_ms()
            ent = sel.item(n - 1).entity        # the newest: a shift/cmd click adds to the end
            kind = _kind(ent)
            cid = _match('click', mods, kind)
            if not cid:
                return                          # leave native selection alone
            token = ''
            if cid in (DOWN_CMD, BOTTOM_CMD):
                target = _owning_occurrence(ent)
                if target is None or not _strict_descendant(design, target):
                    return                      # a pick where you already are
                token = target.entityToken
            _log('click', mods, kind, '->', cid)
            # Deferred so a three-finger tap's second click can veto it.
            _schedule(cid, token)
            if token and n == 1:
                _hide(ent, _last_sel_click)     # navigating, not selecting: no highlight
        except Exception:
            _log('SEL ERR', traceback.format_exc())


class _Click(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            d = json.loads(args.additionalInfo or '{}')
            with fvs.one_step('Navigate'):   # the whole descend is one undo step
                _do_click(d.get('cid', ''), d.get('tok', ''))
        except Exception:
            _log('CLICK ERR', traceback.format_exc())


class _CmdStart(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        global _last_upnav_ms
        if args.commandId in UP_NAV_CMDS:
            _last_upnav_ms = time.time()


# ── the taps, and empty clicks (event-tap file → main thread) ─────────────────
def _do_gesture(payload):
    try:
        ms, gesture, mods = payload.split(' ')[:3]
        mods = [] if mods == '-' else mods.split(',')
        synthetic = payload.split(' ')[3:4] == ['1']
    except ValueError:
        return
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        return
    # A command running: its gestures are its own — except in suspend mode, where the
    # two- and three-finger taps still navigate exactly as they do with nothing open
    # (the nav commands veto themselves at commandStarting and move the view, so the
    # command never sees them). Its picks are left alone: the tap is matched as 'any'.
    in_cmd = False
    try:
        in_cmd = _ui.activeCommand != 'SelectCommand'
    except Exception:
        pass
    if in_cmd:
        if not (fvs.is_suspended() and gesture in ('tap2', 'tap3')):
            return
        cid = _match(gesture, mods, 'any')
        _log(gesture, mods, 'any (suspended, in a command) ->', cid or '(none)')
        # Only the nav commands veto themselves; anything else would end the command.
        cd = _ui.commandDefinitions.itemById(cid) if cid in UP_NAV_CMDS else None
        if cd is not None:
            cd.execute()
        return
    sel = _ui.activeSelections
    if gesture == 'click':
        # Only a click that picked NOTHING comes this way; a pick went the selection
        # path. Fusion has cleared the selection by now, and no selection event acted.
        if sel.count or _last_sel_click == int(ms) or _dblclick_pending():
            return
        kind = 'empty'
    elif gesture == 'tap2' or not synthetic:
        # tap2, and reanchor's 3-finger touch (synthetic 0), never reach Fusion: no pick
        kind = 'any'
    else:
        _abandon()                            # its first click must not act as a click (and
                                              # gets its pick back, so `kind` sees it)
        ent = sel.item(sel.count - 1).entity if sel.count else None
        if isinstance(ent, adsk.fusion.SketchDimension):
            return                            # a double-click edits a dimension's value
        kind = _kind(ent)
    cid = _match(gesture, mods, kind)
    _log(gesture, mods, kind, '->', cid or '(select)')
    if not cid:
        return
    if gesture == 'tap3' and cid == TOP_CMD:
        # Its first click picked whatever was under it; the gesture means "go to the
        # top", not "go to the top and keep that".
        sel.clear()
    cd = _ui.commandDefinitions.itemById(cid)
    if cd is not None:
        cd.execute()


class _Gesture(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            _do_gesture(args.additionalInfo or '')
        except Exception:
            _log('GESTURE ERR', traceback.format_exc())


def _fire(payload):
    try:
        _app.fireCustomEvent(GESTURE_EVENT_ID, payload)
    except Exception:
        pass


def _poll(stop):
    # `stop` is this run's own Event, passed in rather than read from the module: a
    # reload re-execs this file into the same module, and a thread reading the global
    # would pick up the NEW run's Event and outlive its stop().
    last = None
    try:
        last = os.stat(CLICK_EVENT_FILE).st_mtime_ns     # what was there before we ran
    except OSError:
        pass
    while not stop.wait(POLL_S):
        try:
            mt = os.stat(CLICK_EVENT_FILE).st_mtime_ns
        except OSError:
            continue
        if mt == last:
            continue
        last = mt
        try:
            with open(CLICK_EVENT_FILE) as f:
                payload = f.read().strip()
            ms, gesture = payload.split(' ')[:2]
            if time.time() * 1000 - int(ms) > STALE_MS:
                continue
        except Exception:
            continue
        if gesture == 'click':
            # Empty clicks only, and only if a rule wants them: give Fusion time to
            # clear the pick and the selection path time to claim the click first.
            if any(r.get('gesture') == 'click' and r.get('on') == 'empty' for r in _rules()):
                t = threading.Timer(EMPTY_WAIT_S, _fire, (payload,))
                t.daemon = True
                t.start()
        else:
            _fire(payload)


# ── lifecycle ─────────────────────────────────────────────────────────────────
def _register(event_id, handler):
    try:
        evt = _app.registerCustomEvent(event_id)
    except Exception:
        evt = _app.customEvents.itemById(event_id)
    evt.add(handler)
    _handlers.append(handler)
    _events[event_id] = (evt, handler)


def run(context):
    global _app, _ui, _stop
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        _log('=== Click Actions run() ===')
        _register(CLICK_EVENT_ID, _Click())
        _register(GESTURE_EVENT_ID, _Gesture())
        sh = _SelChanged(); _ui.activeSelectionChanged.add(sh); _handlers.append(sh)
        ch = _CmdStart();  _ui.commandStarting.add(ch);        _handlers.append(ch)
        _stop = threading.Event()
        threading.Thread(target=_poll, args=(_stop,), name='ClickActionsPoll',
                         daemon=True).start()
    except Exception:
        if _ui:
            _ui.messageBox('Click Actions run() failed:\n' + traceback.format_exc())


def stop(context):
    try:
        _stop.set()
        _cancel_pending()
        for h in _handlers:
            for evt in (_ui.activeSelectionChanged, _ui.commandStarting):
                try:
                    evt.remove(h)
                except Exception:
                    pass
        for event_id, (evt, h) in list(_events.items()):
            try:
                evt.remove(h)
            except Exception:
                pass
            try:
                _app.unregisterCustomEvent(event_id)
            except Exception:
                pass
        _events.clear()
        _handlers.clear()
    except Exception:
        pass
