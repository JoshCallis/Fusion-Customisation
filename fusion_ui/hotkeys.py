"""
fusion_ui/hotkeys.py — one action registry and one context router for every add-in.

WHY THIS EXISTS

Fusion is generous about letting several commands share a key: the live keymap has
`x` on Cut, Sketch Construction and the browser's parameters toggle all at once, and
it picks between them by whichever is meaningful where you are. What it will not do
is let US say what "meaningful" means. Our add-in commands are always available, so
two of them on one key is a coin toss, and there is nowhere to write down that
"cycle the extrude operation" only makes sense while an extrude is running.

So this module owns that. Every action an add-in wants a key for is DECLARED here
with the domain it belongs to, and one `commandStarting` handler decides — at the
moment the key is pressed — which declared action the key means right now.

    hk = load()                     # see the loader snippet at the bottom
    hk.declare('ExtrudeCycleOpCmd', 'Extrude: Cycle Operation',
               when={'command': 'extrude'}, run=cycle_operation, owner='Extrude')
    hk.attach()                     # once per add-in run(); the handler is a singleton

That is the whole ergonomics. The action now appears in the Shortcuts window with
its domain, and a key can be assigned to it there without disturbing anything else
already on that key.

HOW A KEYPRESS GETS TO THE RIGHT ACTION

Fusion's keymap is still the transport — there is no second keymap and no global
event tap. For each key we care about, Fusion holds ONE binding, the ANCHOR: some
command id that Fusion will fire when the key is pressed. `bindings.json` then
records every action that claims that key and the domain it claims it in.

    press  ->  Fusion fires the anchor id  ->  our commandStarting handler
           ->  sample the context  ->  resolve(seq, ctx)  ->  veto and run the winner

The anchor may be the winner, in which case nothing is re-routed. It may not be, in
which case the anchor is cancelled and the real winner runs instead. Either way,
adding a second action to a key writes bindings.json and touches Fusion's keymap not
at all — which is what makes re-assigning one action never disturb another.

WHY NOT LET FUSION ROUTE IT

Because Fusion's conflict prompt is the only write path, and it decides for itself
whether two commands conflict. Where it agrees with us we get its routing for free
(a native command bound to the same key still wins in its own environment). Where it
does not — two add-in commands, both always available — it would simply take the key
off the first one. Routing on our side is what removes that ceiling, and it is also
the only way an action can be scoped to something Fusion has no concept of, like
"while the extrude dialog is open" or "while the browser window has focus".

RELOAD SAFETY

This module is shared: every add-in that imports it gets the SAME module object out
of sys.modules, and Reload Recent AddIns re-execs it in place (same __dict__) when it
changes. Two consequences are designed for here:

  * State that must not be lost across a re-exec is recovered with
    `globals().get(...)` at the top of the file — the registry, the bindings cache
    and above all the singleton event handler, which stays registered with Fusion
    for the life of the session and must never be created twice.
  * The handler's notify() does nothing but look this module up in sys.modules and
    call `_on_command_starting`, so the routing logic itself hot-reloads even though
    the handler object cannot.
"""

import json, os, sys, threading, time, traceback

import adsk.core

HERE          = os.path.dirname(os.path.abspath(__file__))
CONTEXTS_JSON = os.path.join(HERE, 'contexts.json')
BINDINGS_JSON = os.path.join(HERE, 'bindings.json')
FOCUS_STAMP   = '/tmp/fusion_focus'
# Written by Visibility Snapshot at SketchActivate and removed when the edit ends.
# Borrowed, not duplicated: two different answers to "am I in a sketch" is exactly the
# bug class this whole file exists to avoid.
SKETCH_STAMP  = '/tmp/fusion_sketch_edit_open'
SKETCH_SETTLE = 3.0
LOG           = '/tmp/fusion_hotkeys.log'
# Sampled by remapd's `command` axis so a reserved-chord rewrite stops the instant
# a dialog is open — see fusion_ui/reserved.json and bin/remapd.m's sampleCommand().
COMMAND_STAMP = '/tmp/fusion_command'
# Sampled by remapd's `sketch` axis ('in' / 'out'), published alongside
# COMMAND_STAMP — so Space can mean Cycle Datum in a sketch and Trace Pick outside.
SKETCH_AXIS_STAMP = '/tmp/fusion_sketch'

MODULE_KEY = 'fusion_hotkeys'

# A synthetic Fusion command id that stands in as the anchor when TWO declared
# actions need the same chord — never run itself; _on_command_starting cancels it
# and dispatches to whichever the context names. Must match remap/fusion.py's
# route_id byte for byte (fusion_test.py checks the two agree): a route created
# from either side has to find the same command definition on the other. `seq` here
# is always already canonical (fusion.py's join()/fusion_join() output stored in
# bindings.json's `routes` map), so this only has to split on '+', not canonicalise.
ROUTE_PREFIX = 'JoshRoute_'

_ROUTE_KEY_NAMES = {',': 'comma', '.': 'period', '/': 'slash', ';': 'semicolon',
                    "'": 'quote', '[': 'lbracket', ']': 'rbracket', '\\': 'backslash',
                    '-': 'minus', '=': 'equals', '`': 'backtick'}

# For typing_keys(): the canonical spelling remap/fusion.py::join produces (ctrl,
# alt, shift, cmd order), so the companion window can build the same chord from a
# KeyboardEvent's own modifier flags and compare it byte for byte. bindings.json
# may hold either spelling — Fusion's own words (control, command) from the
# anchor prompt, or the canonical ones from a hand-written row — so this folds
# either into one.
_MOD_ORDER = ('ctrl', 'alt', 'shift', 'cmd', 'fn')
_FUSION_MOD_CANON = {'control': 'ctrl', 'command': 'cmd'}
# remap/remapctl.py's MOD_CANON, which fusion.py::split (and so its route_id) uses.
_ROUTE_MOD_CANON = {'command': 'cmd', 'control': 'ctrl', 'opt': 'alt', 'option': 'alt'}


def _canon_seq(seq):
    parts = [p.strip().lower() for p in str(seq or '').split('+') if p.strip()]
    if not parts:
        return ''
    key = parts[-1]
    mods = {_FUSION_MOD_CANON.get(p, p) for p in parts[:-1]}
    return '+'.join([m for m in _MOD_ORDER if m in mods] + [key])


def route_id(seq):
    """Must match remap/fusion.py::route_id byte for byte — including its mod
    order, which is ALPHABETICAL (split()'s sorted(mods)), not MOD_ORDER; only
    join()'s output spells mods in MOD_ORDER, and route_id builds off split().

    That includes split()'s CANONICAL mod names: the seq arrives in Fusion's own
    spelling ('shift+control+d', from fusion_join), so 'control' must become
    'ctrl' here too. It once didn't — this side registered JoshRoute_ControlShiftD
    while fusion.py anchored JoshRoute_CtrlShiftD, a command that never existed,
    and every routed chord silently did nothing."""
    parts = [p.strip().lower() for p in str(seq or '').split('+') if p.strip()]
    if not parts:
        return ROUTE_PREFIX
    mods = sorted({_ROUTE_MOD_CANON.get(m, m) for m in parts[:-1]})
    key = parts[-1]
    spelled = mods + [_ROUTE_KEY_NAMES.get(key, key)]
    return ROUTE_PREFIX + ''.join(p.capitalize() for p in spelled)

# ── State that survives a re-exec ───────────────────────────────────────────────
# exec_module()/exec(compile(...)) run into this module's EXISTING __dict__, so the
# previous values are still visible here. Recovering them is what keeps one live
# registry and one live event registration across an edit-and-reload cycle.
_registry   = globals().get('_registry')   or {}     # command id -> Action
_dispatch   = globals().get('_dispatch')             # the singleton handler, or None
_terminated = globals().get('_terminated')           # the commandTerminated handler, or None
_bindings   = globals().get('_bindings')             # {action id: sequence} or None
_when_over  = globals().get('_when_over')  or {}     # {action id: domain override}
_typing_over = globals().get('_typing_over') or {}   # {action id: True} - Active While Typing
_routes     = globals().get('_routes')     or {}     # {route id: chord}
_bind_mtime = globals().get('_bind_mtime')  or 0.0
_ctx_cache  = globals().get('_ctx_cache')   or {}
_ctx_time   = globals().get('_ctx_time')    or 0.0
_created    = globals().get('_created')     or {}    # command id -> created handler
_lock       = globals().get('_lock')        or threading.Lock()
_cmd_published    = globals().get('_cmd_published')    # last value written to COMMAND_STAMP
_sketch_published = globals().get('_sketch_published') # last value written to SKETCH_AXIS_STAMP
_modal_depth      = globals().get('_modal_depth')      or 0   # open message / input boxes
_execute_deferred = globals().get('_execute_deferred') or False
_deferred         = globals().get('_deferred')         or []  # native ids the tick should execute
_providers  = globals().get('_providers')  or {}     # shared kind -> {owner: (when, run)}
_direct     = globals().get('_direct')     or {}     # native id -> fn() -> handled? (press only)

CTX_TTL = 0.05          # seconds a sampled context is reused for

_app = adsk.core.Application.get()
_ui  = _app.userInterface


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%s %s\n' % (time.strftime('%H:%M:%S'), ' '.join(str(p) for p in parts)))
    except OSError:
        pass


# ── The domain vocabulary ──────────────────────────────────────────────────────

_vocab = globals().get('_vocab') or {'at': 0.0, 'mtime': None, 'data': None}
VOCAB_RECHECK_S = 0.5   # how stale a read of contexts.json may be before its mtime is checked


def vocabulary():
    """contexts.json, or a bare skeleton if it cannot be read. Kept, and re-read only
    when the file's mtime moves: normalise() asks once per axis per action, so the
    Shortcuts tick's snapshot() parsed it hundreds of times — 120–290 ms a second on
    Fusion's main thread (2026-10-08). Read-only to callers."""
    now = time.monotonic()
    if _vocab['data'] is not None and now - _vocab['at'] < VOCAB_RECHECK_S:
        return _vocab['data']
    try:
        m = os.path.getmtime(CONTEXTS_JSON)
    except OSError:
        m = None
    if _vocab['data'] is None or m is None or m != _vocab['mtime']:
        try:
            with open(CONTEXTS_JSON) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {'axes': {}}
            m = None                       # not kept: the next call tries again
        _vocab['data'], _vocab['mtime'] = data, m
    _vocab['at'] = now
    return _vocab['data']


def _aliases(axis):
    return (vocabulary().get('axes', {}).get(axis, {}) or {}).get('aliases', {}) or {}


def _as_set(v):
    """A constraint value is one string or a list of them; always compare as a set."""
    if v is None:
        return None
    return {v} if isinstance(v, str) else set(v)


def normalise(when):
    """A `when` dict with every value as a set, aliases expanded, unknown axes kept.

    Aliases live in contexts.json so an action can say {'command': 'extrude'} and
    match any of the four ids Fusion uses for an extrude depending on how it was
    started."""
    out = {}
    for axis, val in (when or {}).items():
        vals = _as_set(val)
        if vals is None:
            continue
        alias = _aliases(axis)
        expanded = set()
        for v in vals:
            expanded |= set(alias.get(v, [v]))
        out[axis] = expanded
    return out


def matches(when, ctx):
    """Does a declared domain hold in this context? An axis the action does not
    constrain is free; an axis it constrains must be present and in range."""
    for axis, vals in normalise(when).items():
        if ctx.get(axis) not in vals:
            return False
    return True


def _env_of(when):
    """The `env` values a (raw, un-normalised) `when` implies, lifted through
    contexts.json's `env.implied_by` — an explicit env constraint intersected with
    whatever sketch/command say about it, so an env-scoped action (Extrude's
    Applies In boxes) and an older sketch/command-scoped one still compare
    soundly. None when nothing here constrains env at all; one-directional, so
    the lift never has to invent a negation ('not a dialog')."""
    implied_by = (vocabulary().get('axes', {}).get('env', {}) or {}).get('implied_by', {}) or {}
    per_axis = []
    for axis, val in (when or {}).items():
        vals = _as_set(val)
        if vals is None:
            continue
        if axis == 'env':
            per_axis.append(set(vals))
            continue
        # Every value has to imply something for the axis to say anything: one that
        # does not (a shared action's {'command': ['extrude', 'SmartPatternCmd']} —
        # SmartPatternCmd has no implied_by entry) leaves env open, not narrowed.
        implied = [implied_by.get('%s:%s' % (axis, v)) for v in vals]
        if implied and all(i is not None for i in implied):
            per_axis.append(set().union(*implied))
    if not per_axis:
        return None
    out = set(per_axis[0])
    for s in per_axis[1:]:
        out &= s
    return out


def exclusive(when_a, when_b):
    """(can they share a key, which axes make it safe).

    Two domains are mutually exclusive as soon as ONE axis rules it out — they both
    constrain it and their allowed values are disjoint. Everything else, including
    two actions that constrain nothing, overlaps.

    `env` is checked separately, lifted from sketch/command via _env_of rather than
    compared as a normal axis — it is usually not DECLARED on either side, only
    implied by one that predates it, and a plain axis-by-axis loop cannot see that
    a sketch:in row and an env:[dialog] row disagree."""
    a, b = normalise(when_a), normalise(when_b)
    axes = [ax for ax in a if ax in b and not (a[ax] & b[ax])]
    if not axes:
        ea, eb = _env_of(when_a), _env_of(when_b)
        if ea is not None and eb is not None and not (ea & eb):
            axes = ['env']
    return bool(axes), axes


def specificity(when):
    """How narrow a domain is. Used to pick a winner when two claimants both match:
    the one that said more about where it applies means it more."""
    return len(normalise(when))


def when_of(act):
    """The domain an action currently applies in: what it declared, overridden per
    command by whatever 'Applies in' has written into bindings.json's `when` map.
    The override lives beside the binding, not in the add-in's own source, so
    changing it needs no code and no reload — and for an ADOPTED action, `act.when`
    already IS that override (see _load_bindings), so merging it again is a no-op.

    A shared action's `command` is its providers' — see _declare_shared — and wins
    over anything an older save froze into the override.

    An override that names `env` REPLACES a declared sketch/command that only ever
    spelled env (every value in contexts.json's implied_by — Browser's blanket
    `command: none` is Sketch + Viewport in the boxes). A plain merge could only
    ever narrow it: ticking Dialogs came back unticked and the key stayed dead in
    a dialog, because the declaration's `none` was still underneath."""
    over = _when_over.get(act.id, {})
    base = dict(act.when)
    if 'env' in over:
        implied_by = (vocabulary().get('axes', {}).get('env', {}) or {}).get('implied_by', {}) or {}
        for axis in ('sketch', 'command'):
            vals = _as_set(base.get(axis))
            if vals and all('%s:%s' % (axis, v) in implied_by for v in vals):
                base.pop(axis)
    w = dict(base, **over)
    if act.owner == SHARED_OWNER and 'command' in act.when:
        w['command'] = act.when['command']
    return w


def typing_of(cid):
    """Is Active While Typing on for this action's binding? Off by default, same
    as an unset `when` override — a plain per-command flag, not a domain, so
    there is nothing to merge with a declared default the way `when_of` merges."""
    return bool(_typing_over.get(cid))


# ── Sampling the live context ──────────────────────────────────────────────────

_IDLE_COMMANDS = {'', 'SelectCommand', 'SelectionCommand', 'FusionSelectCommand'}


def _is_idle(cid):
    """Whether `cid` means "no command owns the screen" — the fold that defines
    'none' for the command axis, for both the sampler and COMMAND_STAMP.

    The nav aliases are in here because a camera move is not a command in the sense
    this axis exists to capture: the axis is for keys that mean something INSIDE a
    dialog (see contexts.json), and an orbit has no dialog, no field and nothing to
    type into. Left out, they broke the reserved-key rewrites instead — Fusion holds
    ResetOrbitCenterCommand as the active command for a few hundred ms after every
    trackpad orbit, so a key pressed in that window read as "a command is running",
    fusion-idle missed, and the BARE key went to Fusion. For `s` that is Fusion's own
    search: the exact symptom, and intermittent purely because it depended on how
    soon after an orbit you pressed. The list is data in contexts.json so the next
    id Fusion invents is a config edit, not a code one."""
    if cid in _IDLE_COMMANDS or cid in _aliases('command').get('nav', ()):
        return True
    # The Browser's forwarded actions (actions.json, home 'browser') are vetoed before
    # they open anything, and no commandTerminated follows a veto — so published as
    # themselves they stood until the next tick, and a key pressed again inside that
    # second missed fusion-idle: every other Return went to Fusion bare.
    return cid in _browser_homed()


_homed_cache = globals().get('_homed_cache') or {'mtime': None, 'ids': frozenset()}


def _browser_homed():
    path = os.path.join(HERE, 'actions.json')
    try:
        m = os.path.getmtime(path)
        if m != _homed_cache['mtime']:
            with open(path) as f:
                acts = json.load(f).get('actions') or []
            _homed_cache.update(mtime=m, ids=frozenset(
                a.get('id') for a in acts if a.get('home') == 'browser'))
    except (OSError, ValueError):
        pass
    return _homed_cache['ids']

_WORKSPACE_NAMES = {
    'FusionSolidEnvironment':      'design',
    'FusionRenderEnvironment':     'render',
    'FusionDrawingEnvironment':    'drawing',
    'CAMEnvironment':              'cam',
    'SimulationEnvironment':       'sim',
    'GenerativeEnvironment':       'generative',
    'ElectronDesignWorkspace':     'electronics',
    'FusionAnimationEnvironment':  'animation',
}

# objectType tails, longest first so 'SketchDimension' never matches as 'Sketch'.
_SELECTION_KINDS = [
    ('SketchDimension', 'dimension'),
    ('SketchCurve',     'sketchcurve'),
    ('SketchLine',      'sketchcurve'),
    ('SketchPoint',     'vertex'),
    ('Occurrence',      'component'),
    ('Component',       'component'),
    ('BRepBody',        'body'),
    ('BRepFace',        'face'),
    ('BRepEdge',        'edge'),
    ('BRepVertex',      'vertex'),
    ('Sketch',          'sketch'),
    ('Parameter',       'parameter'),
]


# What an axis reads as when it cannot be sampled. These are REAL values, not a
# shrug: an axis that fails must still name the ordinary case, or every action scoped
# to that ordinary case silently stops matching. ('other' for workspace is genuine —
# it is one of the declared values.)
_AXIS_DEFAULT = {
    'workspace': 'other',
    'sketch':    'out',
    'command':   'none',
    'focus':     'viewport',
    'selection': 'none',
    'env':       'viewport',
}


def _sample(axis):
    """One axis, sampled from the live application. Every sampler is cheap and every
    one is allowed to fail — this runs on the main thread inside commandStarting, so
    a raising API call must never take a keystroke down with it."""
    try:
        if axis == 'workspace':
            wid = _ui.activeWorkspace.id
            return _WORKSPACE_NAMES.get(wid, wid or 'other')

        if axis == 'sketch':
            # The same test Visibility Snapshot's is_editing_sketch() uses, and for the
            # same reasons: SketchStop is enabled exactly while a sketch is open at any
            # depth and never raises, where design.activeEditObject raises outright for
            # a sketch nested in an occurrence. isEnabled lives on the CONTROL
            # definition, not the command definition.
            cd = _ui.commandDefinitions.itemById('SketchStop')
            if cd is not None and cd.controlDefinition.isEnabled:
                return 'in'
            # ...and it stays False for a beat after the sketch actually opened. The
            # stamp Visibility Snapshot writes at SketchActivate covers that gap; same
            # file and same window, so the two never disagree about where you are.
            try:
                if time.time() - os.path.getmtime(SKETCH_STAMP) < SKETCH_SETTLE:
                    return 'in'
            except OSError:
                pass
            return 'out'

        if axis == 'command':
            cid = _ui.activeCommand or ''
            return 'none' if _is_idle(cid) else cid

        if axis == 'env':
            # DERIVED, not sampled on its own - see contexts.json's env entry.
            # Read through context()'s own cache rather than _ui directly, so a
            # request that already wanted 'command' or 'sketch' this tick never
            # samples either twice.
            cid = context(['command'])['command']
            if cid != 'none' and _is_dialog(cid):
                return 'dialog'
            return 'sketch' if context(['sketch'])['sketch'] == 'in' else 'viewport'

        if axis == 'focus':
            # Written by whichever companion window holds key focus and refreshed
            # while it keeps it. Missing or stale means the keystroke reached Fusion,
            # which is also the case before anything writes the stamp at all.
            try:
                fresh = time.time() - os.path.getmtime(FOCUS_STAMP) < 3.0
            except OSError:
                fresh = False
            if fresh:
                with open(FOCUS_STAMP) as f:
                    return (f.read().strip() or 'viewport')
            return 'viewport'

        if axis == 'selection':
            sels = _ui.activeSelections
            if sels.count == 0:
                return 'none'
            t = sels.item(0).entity.objectType or ''
            for tail, kind in _SELECTION_KINDS:
                if t.endswith(tail):
                    return kind
            return 'other'
    except Exception:
        # Worth knowing about: a sampler that raises every press turns its axis into
        # the default and quietly stops a whole domain matching.
        _log('axis', axis, 'failed:', traceback.format_exc().replace('\n', ' | '))
    return _AXIS_DEFAULT.get(axis, 'other')


def context(axes=None, force=False):
    """The live context, sampled LAZILY: only the axes something actually asks about,
    cached for a beat so one keypress never samples the same axis twice."""
    global _ctx_cache, _ctx_time
    now = time.time()
    if force or now - _ctx_time > CTX_TTL:
        _ctx_cache = {}
        _ctx_time = now
    wanted = axes if axes is not None else list(vocabulary().get('axes', {}))
    for axis in wanted:
        if axis not in _ctx_cache:
            _ctx_cache[axis] = _sample(axis)
    return dict(_ctx_cache)


# ── The registry ───────────────────────────────────────────────────────────────

class Action(object):
    """One thing a key can be pointed at.

    `native` marks a claimant implemented SOMEWHERE ELSE — by Fusion, or by another
    add-in's own handler. We never run those and never cancel them. Declaring one
    only tells the router that this key already means something in that domain, so
    our claimants know to stand aside, and so the key has an anchor we can hang more
    claims on. It is inert until something else claims the same key."""

    __slots__ = ('id', 'name', 'desc', 'group', 'owner', 'when',
                 'run', 'veto', 'priority', 'native', 'seq_hint', 'declared', 'adopted')

    def __init__(self, id, name, desc='', group='', owner='', when=None,
                 run=None, veto=True, priority=0, native=False, seq_hint='',
                 adopted=False):
        self.id       = id
        self.name     = name or id
        self.desc     = desc
        self.group    = group or owner
        self.owner    = owner
        self.when     = when or {}
        self.run      = run
        self.veto     = veto
        self.priority = priority
        self.native   = native
        self.seq_hint = seq_hint
        self.declared = time.time()
        # Distinct from `native`: a declared action can be native on purpose
        # (Extrude and Browser declare native=True commands deliberately — this
        # module never ran or vetoed those). `adopted` means WE never declared
        # it at all — _load_bindings() found a bound id with no Action behind it
        # and filed one in so a domain override has somewhere to live.
        self.adopted  = adopted

    def invoke(self):
        if self.run is None:
            return
        try:
            self.run()
        except Exception:
            _log('action failed', self.id, traceback.format_exc().replace('\n', ' | '))

    def as_dict(self):
        w = when_of(self)
        return {
            'id': self.id, 'name': self.name, 'desc': self.desc,
            'group': self.group, 'owner': self.owner,
            'when': {k: sorted(v) for k, v in normalise(w).items()},
            'whenRaw': w,
            'native': self.native, 'adopted': self.adopted, 'priority': self.priority,
            'specificity': specificity(w),
            'seq': binding_of(self.id),
            'typing': typing_of(self.id),
        }


def declare(id, name='', desc='', group='', owner='', when=None,
            run=None, veto=True, priority=0, native=False, register=True):
    """Declare an action and (unless it is native) give Fusion a command definition
    for it so a key can be bound to it at all.

    Re-declaring the same id replaces it — that is what an add-in reload does, and
    it must not leave the old callable behind."""
    act = Action(id, name, desc, group, owner, when, run, veto, priority, native)
    with _lock:
        _registry[id] = act
    if register and not native:
        _register_definition(act)
    return act


def withdraw(owner):
    """Drop every action an add-in declared. Call from its stop(). The command
    DEFINITIONS are deliberately left alone: Fusion keys bindings by id, so deleting
    one would throw the shortcut away (see the reload/shortcut-safe pattern).

    Its providers of SHARED actions go too, and each shared action it provided is
    re-declared over whoever is left."""
    with _lock:
        for cid in [k for k, a in _registry.items() if a.owner == owner]:
            del _registry[cid]
            _direct.pop(cid, None)
        kinds = [k for k, provs in _providers.items() if owner in provs]
        for k in kinds:
            del _providers[k][owner]
    for k in kinds:
        _declare_shared(k)


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    """Fallback only. The router cancels these commands in commandStarting, long
    before one is ever created — but a veto that failed to take would otherwise leave
    an empty command dialog standing and the action not run at all. Registered once
    per definition, and it looks the action up at call time so a reload is picked up.

    It cannot double-fire with the router: cancelling the command in commandStarting
    is precisely what stops commandCreated from happening."""

    def __init__(self, cid):
        super().__init__()
        self.cid = cid

    def notify(self, args):
        try:
            mod = sys.modules.get(MODULE_KEY)
            act = mod.get(self.cid) if mod else None
            if act is not None and act.run is not None:
                act.invoke()
        except Exception:
            _log('fallback failed', self.cid, traceback.format_exc().replace('\n', ' | '))


def _register_definition(act):
    """Find-or-create the CommandDefinition. No toolbar control — these exist purely
    so Fusion has something to hang a key on and something to list in its keyboard
    panel. Never deleted, never re-created: Fusion keys bindings by id, and deleting
    the definition is what throws a shortcut away."""
    try:
        cd = _ui.commandDefinitions.itemById(act.id)
        if not cd:
            cd = _ui.commandDefinitions.addButtonDefinition(
                act.id, act.name, act.desc or act.name)
        if act.id not in _created:
            h = _CreatedHandler(act.id)
            cd.commandCreated.add(h)
            _created[act.id] = h          # kept alive here; Fusion holds the pointer
    except Exception:
        _log('could not register', act.id, traceback.format_exc().replace('\n', ' | '))


def actions():
    with _lock:
        return list(_registry.values())


def get(cid):
    with _lock:
        return _registry.get(cid)


# ── Shared actions: one action, a provider per command ─────────────────────────
#
# "Cycle Direction" means the same thing in Extrude, Rectangular Pattern and Mirror —
# step the running command's direction row on one — so it is ONE action with ONE key,
# and each command PROVIDES what it means there:
#
#     hk.provide('direction', owner='Extrude', when={'command': 'extrude'},
#                run=_cycle_direction)
#
# The shared action's domain is the union of its providers' commands, so it claims
# its key only while one of them is running and shares it freely with anything
# scoped to `command: none`. At press time the provider whose `when` holds runs. A
# new command that has a direction joins by providing; nothing here changes.
#
# Every provider's `when` must say `command` — that is what the union is built from.
#
# While any provider's command is running, COMMAND_STAMP reads 'feature' (see
# _publish_command), so remapd has ONE situation for "in a feature dialog" rather
# than one per command — and the `env` axis reads 'dialog' (_is_dialog), so Applies
# In has ONE Dialogs box rather than one per command. A key that should mean
# something inside a dialog is bound once with Dialogs ticked; what it means in
# Mirror versus Extrude is each dialog's own provider, never a checkbox.
#
# The ids are the ones Extrude's own cycles had: Fusion keys a shortcut by command id,
# and those two already hold their keys in Fusion's keymap. Renaming them would throw
# the keys away (the reload/shortcut-safe rule), so only the NAME is shared.

SHARED = {
    'direction': ('ExtrudeCycleDirectionCmd', 'Cycle Direction',
                  'Step the running command\'s direction on one — Extrude: one side, '
                  'two sides, symmetric; Rectangular Pattern: its axes, then the '
                  'picked edge; Mirror: X, Y, Z, then the picked plane.'),
    'operation': ('ExtrudeCycleOperationCmd', 'Cycle Operation',
                  'Step the running command\'s operation on one — Extrude: auto, '
                  'join, cut, intersect, new body; Combine: join, cut, intersect; '
                  'Mirror: join, new body.'),
    # The same two rings stepped the other way. New ids (no key held them before).
    'direction_back': ('CycleDirectionBackCmd', 'Cycle Direction Back',
                       'Step the running command\'s direction back one — the Cycle '
                       'Direction ring, reversed.'),
    'operation_back': ('CycleOperationBackCmd', 'Cycle Operation Back',
                       'Step the running command\'s operation back one — the Cycle '
                       'Operation ring, reversed.'),
}
SHARED_OWNER = 'Shared'


def provide(kind, owner, when, run):
    """Say what the shared action `kind` means while `when` holds. Re-providing for
    the same owner replaces it (a reload); withdraw(owner) takes it away."""
    if kind not in SHARED:
        raise KeyError('no shared action %r' % kind)
    with _lock:
        _providers.setdefault(kind, {})[owner] = (dict(when or {}), run)
    return _declare_shared(kind)


def providers(kind):
    with _lock:
        return dict(_providers.get(kind) or {})


def feature_commands():
    """Every command id some shared action has a provider for — what 'feature' in
    COMMAND_STAMP means."""
    out = set()
    for kind in SHARED:
        for w, _run in providers(kind).values():
            out |= normalise({'command': w.get('command')}).get('command') or set()
    return out


def _is_dialog(cid):
    """Is `cid` one of OUR dialogs — the `env` axis's 'dialog'? Every command a shared
    action has a provider for, their aliases (Extrude's four ids), and the shared
    actions' own ids (a cycle key is vetoed the moment it starts, and must still read
    as inside the dialog it was pressed in). The same set _publish_command folds to
    'feature' for remapd, so the two sides never disagree about 'in a dialog'."""
    if cid in {s[0] for s in SHARED.values()}:
        return True
    return cid in feature_commands()


def _declare_shared(kind):
    """(Re-)declare the shared action over its current providers, or drop it from the
    registry when there are none left (its definition stays — see withdraw).

    Its domain is `env: dialog` — the one box Applies In shows ticked — AND the union
    of its providers' commands. The command half is never the user's to edit: which
    dialogs a cycle means something in is the dialogs' own say (each one providing),
    so when_of() keeps it out of the override and a new provider joins on its own."""
    cid, name, desc = SHARED[kind]
    provs = providers(kind)
    if not provs:
        with _lock:
            _registry.pop(cid, None)
        return None
    commands = set()
    for w, _run in provs.values():
        commands |= _as_set(w.get('command')) or set()
    act = declare(cid, name, desc, group=SHARED_OWNER, owner=SHARED_OWNER,
                  when={'env': ['dialog'], 'command': sorted(commands)},
                  run=lambda k=kind: _run_shared(k))
    try:
        # The definition predates the sharing ("Extrude: Cycle Direction") and is
        # never re-created, so the name Fusion's keyboard panel shows is set here.
        cd = _ui.commandDefinitions.itemById(cid)
        if cd and cd.name != name:
            cd.name = name
            cd.tooltip = desc
    except Exception:
        pass
    return act


def _run_shared(kind):
    """Run the provider whose domain holds right now — the most specific, then the
    first provided. Two providers on one command is a mistake, not a feature."""
    provs = list(providers(kind).items())
    axes = set()
    for _o, (w, _r) in provs:
        axes |= set(normalise(w))
    ctx = context(sorted(axes))
    live = [(o, w, r) for o, (w, r) in provs if matches(w, ctx)]
    if not live:
        _log('shared', kind, 'no provider in', ctx)
        return
    live.sort(key=lambda t: -specificity(t[1]))
    owner, _w, run = live[0]
    try:
        run()
    except Exception:
        _log('shared', kind, owner, 'failed', traceback.format_exc().replace('\n', ' | '))


# ── Bindings: which key each action claims ─────────────────────────────────────

def _load_bindings():
    """{action id: sequence}, plus this module's `_when_over` (per-command domain
    overrides) and `_routes` (route id -> chord) from the same document. Re-read
    when the file changes, so the Shortcuts window can write an assignment and
    have it take effect on the very next keypress with no reload and no round
    trip through Fusion.

    A bound id with no Action behind it is ADOPTED as a native one: Fusion is
    already firing it on its own keymap (that is what a claim in bindings.json
    with no declare() means), and the only thing missing was somewhere for
    'Applies in' to write a domain override. An adopted id whose claim later
    goes — unbound, or the row removed — is dropped the same way withdraw()
    drops an add-in's own actions; it was never ours to keep around."""
    global _bindings, _when_over, _typing_over, _routes, _bind_mtime
    try:
        m = os.path.getmtime(BINDINGS_JSON)
    except OSError:
        if _bindings is None:
            _bindings, _when_over, _typing_over, _routes = {}, {}, {}, {}
        return _bindings
    if _bindings is None or m != _bind_mtime:
        try:
            with open(BINDINGS_JSON) as f:
                data = json.load(f)
            _bindings = {k: v for k, v in (data.get('bindings') or {}).items() if v}
            _when_over = {k: v for k, v in (data.get('when') or {}).items() if v}
            _typing_over = {k: v for k, v in (data.get('typing') or {}).items() if v}
            # Keyed by what route_id() says NOW, not what the file says: a route
            # written under an older spelling of its id is still the same chord,
            # and the id Fusion fires is the one fusion.py anchored.
            _routes = {route_id(s): s for s in (data.get('routes') or {}).values() if s}
        except (OSError, ValueError):
            _bindings = _bindings or {}
            _when_over = _when_over or {}
            _typing_over = _typing_over or {}
            _routes = _routes or {}
        _bind_mtime = m

        with _lock:
            for cid, seq in _bindings.items():
                if not seq or cid in _registry or cid.startswith(ROUTE_PREFIX):
                    continue
                _registry[cid] = Action(cid, cid, when=_when_over.get(cid) or {},
                                        native=True, adopted=True)
            for cid in [k for k, a in _registry.items()
                       if a.adopted and not _bindings.get(k)]:
                del _registry[cid]

        for rid in list(_routes):
            _ensure_route_def(rid)
    return _bindings


def bindings():
    return dict(_load_bindings())


def binding_of(cid):
    return _load_bindings().get(cid, '')


def claimants(seq):
    """Every declared action that claims this key, most specific first.

    Compared canonically (_canon_seq): bindings.json holds Fusion's spelling
    ('shift+alt+control+r') but a companion window's /press sends typing_keys'
    ('ctrl+alt+shift+r'). A byte compare let every Active While Typing chord from
    the Extrude panel resolve to nothing."""
    b = _load_bindings()
    want = _canon_seq(seq)
    out = [a for a in actions() if b.get(a.id) and _canon_seq(b.get(a.id)) == want]
    out.sort(key=lambda a: (-specificity(when_of(a)), -a.priority, -a.declared))
    return out


def resolve(seq, ctx=None):
    """The action a key means right now, or None if it means nothing here."""
    cands = claimants(seq)
    if not cands:
        return None
    axes = set()
    for a in cands:
        axes |= set(normalise(when_of(a)))
    ctx = ctx if ctx is not None else context(sorted(axes))
    for a in cands:
        if matches(when_of(a), ctx):
            return a
    return None


def overridden(cid):
    """True when the id that just fired is NOT what its key means right now — the
    router is about to run something else instead.

    Add-ins that keep their own commandStarting handler call this and stand aside when
    it says so. Several handlers see the same event and the order they run in is not
    defined, so each has to decline independently: vetoing is idempotent, but ACTING on
    a key that was not for you is not."""
    act = get(cid)
    if act is None:
        return False
    seq = binding_of(cid)
    if not seq:
        return False
    winner = resolve(seq)
    return winner is not None and winner is not act


def conflicts(seq, when):
    """Which existing claimants of this key a new domain would collide with.

    Returns [] when the key is free to take — every current claimant is mutually
    exclusive with the proposed domain — and otherwise the ones that overlap, each
    with the reason, so the window can say what it would fight with rather than
    just refusing."""
    out = []
    for a in claimants(seq):
        w = when_of(a)
        ok, axes = exclusive(w, when)
        if not ok:
            out.append({'id': a.id, 'name': a.name,
                        'when': {k: sorted(v) for k, v in normalise(w).items()}})
    return out


def press(seq, any_key=False):
    """Run this chord's live winner, but only if Active While Typing is on for it
    — the entry point for a keystroke that never touches Fusion's keymap at all
    because it landed in a companion window's text field instead (fusion_value's
    extrude panel: see its keydown hook and /press). Mirrors the re-route path in
    _on_command_starting: a native winner is executed, one of ours is invoked.

    `any_key`: the keystroke landed in a panel with NO text field (the mirror
    panel), where there is nothing to type into and so nothing for Active While
    Typing to protect — every live winner runs, exactly as it would have if
    Fusion had had the keyboard. See panel_keys."""
    winner = resolve(seq)
    if winner is None or not (any_key or typing_of(winner.id)):
        return
    if winner.native:
        if _run_direct(winner.id):
            return
        _execute_or_defer(winner.id)
    else:
        winner.invoke()


def set_direct(cid, fn):
    """Let press() run the native action `cid` without starting a Fusion command:
    `fn()` does what the add-in's own commandStarting handler would do for it and
    returns True if it did. For actions whose command only exists to be vetoed —
    the Browser's window-homed ones. press() otherwise has to execute() the command
    for the add-in to veto, and execute() while a feature dialog is running proved
    unreliable (3-finger F → Suspend in the FusionValue panel, 2026-10-01), where
    the non-native cycles, invoked directly, never missed. None removes it."""
    with _lock:
        if fn is None:
            _direct.pop(cid, None)
        else:
            _direct[cid] = fn


def _run_direct(cid):
    with _lock:
        fn = _direct.get(cid)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        _log('direct failed', cid, traceback.format_exc().replace('\n', ' | '))
        return False


def typing_keys(ctx=None):
    """Every bound chord whose live winner (in `ctx`, default the live context)
    has Active While Typing on — what a companion window should hand a keystroke
    to rather than let it type as a character. Canonical spelling (_canon_seq),
    matching the order the window's own keydown hook builds a chord in."""
    out = set()
    for seq in set(bindings().values()) | set(_routes.values()):
        if not seq:
            continue
        winner = resolve(seq, ctx)
        if winner is not None and typing_of(winner.id):
            out.add(_canon_seq(seq))
    return sorted(out)


def cycle_keys(ctx=None):
    """{chord: shared kind} for every bound chord whose live winner is a SHARED
    action ('operation', 'direction') — so a companion panel can step its own row
    the instant the key goes down, rather than wait for the round trip through
    press() and the add-in's tick (behind whatever preview is computing) before the
    row moves. The panel posts the stepped value as an ordinary choice; the chord
    still has to be in typing_keys / panel_keys for the panel to take it at all.
    Same spelling as typing_keys."""
    ids = {v[0]: k for k, v in SHARED.items()}
    out = {}
    for seq in set(bindings().values()) | set(_routes.values()):
        if not seq:
            continue
        winner = resolve(seq, ctx)
        if winner is not None and winner.id in ids:
            out[_canon_seq(seq)] = ids[winner.id]
    return out


def panel_keys(ctx=None):
    """Every bound chord with a live winner at all — typing_keys without the Active
    While Typing test, for a companion panel that stands in for a hidden command
    dialog and has no text field (the mirror panel). It holds the keyboard while it
    is up, so Fusion's keymap sees nothing; handing over every chord that means
    something right now is what makes the panel behave as if Fusion still had the
    keyboard. Same spelling as typing_keys; pressed with press(seq, any_key=True)."""
    out = set()
    for seq in set(bindings().values()) | set(_routes.values()):
        if seq and resolve(seq, ctx) is not None:
            out.add(_canon_seq(seq))
    return sorted(out)


# ── The router ─────────────────────────────────────────────────────────────────

def _on_command_starting(args):
    """Called for EVERY command Fusion starts. Looked up through sys.modules by the
    handler, so this function — the whole routing decision — hot-reloads."""
    cid = args.commandId
    _publish_command(cid)

    if cid.startswith(ROUTE_PREFIX):
        # A route never runs itself — it exists only so Fusion has something to
        # anchor a shared chord to. Cancel it before Fusion gets anywhere near
        # creating the command, and dispatch to whichever claimant the live
        # context actually names.
        args.isCanceled = True
        seq = _routes.get(cid) or ''
        winner = resolve(seq) if seq else None
        if winner is not None:
            if winner.native:
                _execute_or_defer(winner.id)
            else:
                winner.invoke()
        return

    act = get(cid)
    if act is None:
        return                      # nothing to do with us

    seq = binding_of(cid)
    if not seq:
        # Not bound through us, so this is a toolbar press, a script, or a key set
        # in Fusion's own panel. Run it in its own terms; there is nothing to route.
        if act.run and act.veto:
            args.isCanceled = True
        act.invoke()
        return

    winner = resolve(seq)
    if winner is None:
        # The key is bound, but nothing claims it here. If the command that fired is
        # Fusion's own, leave it alone — adopting a native binding into the registry
        # must never change what that key already did. If it is one of ours, swallow
        # it: an action outside its domain should do nothing, not something wrong.
        # …unless "Applies in" scoped it: a native command the user limited to a
        # domain (one-finger F, Viewport only) must be inert outside it, or the
        # scope only ever steered between OUR actions and Fusion's own still fired.
        if not act.native or _when_over.get(cid):
            args.isCanceled = True
        return

    if winner is act:
        if act.native:
            return                  # Fusion's own command; let it through untouched
        if act.veto:
            args.isCanceled = True
        act.invoke()
        return

    # Re-route: the anchor fired, but this key means something else where we are.
    args.isCanceled = True
    if winner.native:
        _execute_or_defer(winner.id)
    else:
        winner.invoke()


def _execute(cid):
    try:
        cd = _ui.commandDefinitions.itemById(cid)
        if cd:
            cd.execute()
    except Exception:
        _log('could not execute', cid, traceback.format_exc().replace('\n', ' | '))


def _execute_or_defer(cid):
    """Run a native command as the resolved winner. This re-route path — calling
    CommandDefinition.execute() on ANOTHER command from inside commandStarting —
    has never been exercised live; if it turns out execute() silently does
    nothing from in here, flip `_execute_deferred` at the top of the module and
    the add-in's own tick drains `_deferred` outside the handler instead."""
    if _execute_deferred:
        _deferred.append(cid)
    else:
        _execute(cid)


def _stamp_says(path, val):
    """Whether the stamp on disk still reads `val`. Checked before trusting
    "unchanged" and only touching the mtime: something else can rewrite the file
    (an offline test once left 'JoshRoute_AltCtrl9' in COMMAND_STAMP), and a
    touch keeps a wrong value looking fresh to remapd indefinitely."""
    try:
        with open(path) as f:
            return f.read() == val
    except OSError:
        return False


def _publish_command(cid):
    """Write the id Fusion just started to COMMAND_STAMP, folded through
    _is_idle the same as the `command` context axis (so 'none' means the
    same thing here as it does to context()) — remapd's `command` axis reads this
    to hold off a reserved-chord rewrite while a dialog is open. Runs on every
    command start, but only WRITES on change; otherwise just touches the mtime,
    which is all remapd's staleness check needs.

    Every command a shared action has a provider for (Extrude, Mirror, Rectangular
    Pattern, Combine…) is folded to the single word 'feature', so ONE remapd
    situation (fusion-feature-*) means "a feature dialog is up" without knowing
    any ids. So are the shared actions' own ids: a cycle key is vetoed the moment
    it starts, and must not read as some other command until the next tick.
    fusion-idle is the only situation that still ever looks for 'none'. The
    dimension aliases fold to 'dimension' the same way (fusion-dimension)."""
    global _cmd_published
    _publish_sketch()
    if _modal_depth > 0:
        val = 'modal'                    # a message / input box has the keyboard
    elif not _is_idle(cid) and (cid in feature_commands()
                                or cid in {s[0] for s in SHARED.values()}):
        val = 'feature'
    elif not _is_idle(cid) and cid in _aliases('command').get('dimension', ()):
        # Folded for fusion-dimension: Space has to reach Smart Dimension's cycle
        # WHILE the dimension tool runs, and the vetoed SmartDimensionCmd itself
        # must not read as some other command for the tick before it is corrected.
        val = 'dimension'
    elif not _is_idle(cid) and cid in _aliases('command').get('constraint', ()):
        # fusion-constraint: Space ties the pick to a bound in H/V and Coincident.
        val = 'constraint'
    else:
        val = 'none' if _is_idle(cid) else cid
    if val == _cmd_published and _stamp_says(COMMAND_STAMP, val):
        try:
            os.utime(COMMAND_STAMP, None)
        except OSError:
            pass
        return
    tmp = COMMAND_STAMP + '.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write(val)
        os.replace(tmp, COMMAND_STAMP)
        _cmd_published = val
    except OSError:
        pass


def _publish_sketch():
    """SKETCH_AXIS_STAMP for remapd's `sketch` axis: 'in' while a sketch is open,
    else 'out' — _sample('sketch')'s answer, settle window included. Called from
    _publish_command, so it rides the same commandStarting/Terminated events and
    the same once-a-second tick; write on change, otherwise just touch the mtime
    (remapd reads a stale file as 'unknown', never as 'in')."""
    global _sketch_published
    val = _sample('sketch')
    if val == _sketch_published and _stamp_says(SKETCH_AXIS_STAMP, val):
        try:
            os.utime(SKETCH_AXIS_STAMP, None)
            return
        except OSError:
            pass
    tmp = SKETCH_AXIS_STAMP + '.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write(val)
        os.replace(tmp, SKETCH_AXIS_STAMP)
        _sketch_published = val
    except OSError:
        pass


# ── Modal boxes ───────────────────────────────────────────────────────────────
# ui.messageBox / ui.inputBox run no command, so to the `command` axis Fusion looks
# IDLE while one is up — and a reserved rewrite (Return → Browser: Edit, Space) would
# take the key from its OK button. Every call from any add-in goes through the one
# UserInterface class, so attach() wraps both there, once per session: while a box is
# open COMMAND_STAMP says 'modal', which is not 'none', and remapd lets the key through.
# The wrapper looks this module up at call time, so the logic hot-reloads.

def _modal_enter():
    global _modal_depth
    _modal_depth += 1
    _publish_command('')


def _modal_exit():
    global _modal_depth
    _modal_depth = max(0, _modal_depth - 1)
    try:
        cid = _ui.activeCommand or ''
    except Exception:
        cid = ''
    _publish_command(cid)


def _wrap_modal(name):
    cls = adsk.core.UserInterface
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, '_josh_modal', False):
        return

    def wrapped(self, *a, **kw):
        mod = sys.modules.get(MODULE_KEY)
        try:
            if mod:
                mod._modal_enter()
        except Exception:
            mod = None
        try:
            return orig(self, *a, **kw)
        finally:
            try:
                if mod:
                    mod._modal_exit()
            except Exception:
                pass
    wrapped._josh_modal = True
    wrapped.__doc__ = orig.__doc__
    setattr(cls, name, wrapped)


def refresh_command():
    """Sample the live command and republish. The add-in's periodic tick calls
    this so COMMAND_STAMP's mtime keeps moving even when nothing starts a new
    command for a while — remapd's 3s staleness window must never lapse just
    because the user is sitting idle in the viewport."""
    try:
        cid = _ui.activeCommand or ''
    except Exception:
        cid = ''
    _publish_command(cid)


class _StartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Deliberately empty of logic. Looking the module up at call time is what lets
    the routing change without re-registering (and without double-firing) the
    handler — the object itself lives for the whole Fusion session."""

    def notify(self, args):
        try:
            mod = sys.modules.get(MODULE_KEY)
            if mod:
                mod._on_command_starting(args)
        except Exception:
            _log('dispatch failed', traceback.format_exc().replace('\n', ' | '))


class _TerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """Mirrors _StartingHandler: fires when a command ENDS, so COMMAND_STAMP drops
    back to 'none' the instant a dialog closes instead of waiting on whatever
    command happens to start next — which may be a long while, or never, if the
    user just goes back to idling in the viewport."""

    def notify(self, args):
        try:
            mod = sys.modules.get(MODULE_KEY)
            if mod:
                mod._publish_command('none')
        except Exception:
            _log('terminated dispatch failed', traceback.format_exc().replace('\n', ' | '))


def attach():
    """Install the router. Safe to call from every add-in's run(), every reload: the
    handlers are created and registered exactly once per Fusion session."""
    global _dispatch, _terminated
    if _dispatch is None:
        _dispatch = _StartingHandler()
        try:
            _ui.commandStarting.add(_dispatch)
        except Exception:
            _log('could not attach', traceback.format_exc().replace('\n', ' | '))
            _dispatch = None
    for name in ('messageBox', 'inputBox'):
        try:
            _wrap_modal(name)
        except Exception:
            _log('could not wrap', name, traceback.format_exc().replace('\n', ' | '))
    if _terminated is None:
        _terminated = _TerminatedHandler()
        try:
            _ui.commandTerminated.add(_terminated)
        except Exception:
            _log('could not attach terminated', traceback.format_exc().replace('\n', ' | '))
            _terminated = None
    return _dispatch


# ── For the Shortcuts window ───────────────────────────────────────────────────

def snapshot():
    """Everything the Shortcuts window needs to draw the registry: the actions with
    their domains, the current assignments, the axis vocabulary, and — so a domain
    can be shown as live or not while you are looking at it — the current context."""
    acts = [a.as_dict() for a in actions()]
    keys = {}
    for a in acts:
        if a['seq']:
            keys.setdefault(a['seq'], []).append(a['id'])
    return {
        'actions':  acts,
        'bindings': bindings(),
        'keys':     keys,
        'axes':     vocabulary().get('axes', {}),
        'context':  context(),
        'routes':   dict(_routes or {}),
    }


def _write_doc(bindings_map, when_map, routes_map, typing_map=None):
    """Rewrite the WHOLE document — bindings, when overrides, typing overrides and
    routes together — so a save from any one of set_binding()/ensure_route() never
    drops what the others already hold. The old set_binding() rebuilt
    {'bindings': …} alone and would have silently thrown `when`/`routes` away the
    next time it ran. `typing_map=None` keeps whatever is already on disk, the
    same as every other caller here that isn't touching that map."""
    data = {'bindings': bindings_map, 'when': when_map, 'routes': routes_map,
            'typing': typing_map if typing_map is not None else dict(_typing_over or {})}
    tmp = BINDINGS_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, BINDINGS_JSON)
    _load_bindings()


def set_binding(cid, seq, when=None, typing=None):
    """Point one action at one key (or at nothing, with seq=''), and optionally
    set its domain override: when={} clears one, when=None leaves whatever is
    there alone. `typing` is the same tri-state, boolean rather than a dict: None
    leaves it, False clears it, True sets it. Touches only this action's row and
    its own overrides — nothing else on that key, or anyone else's override, is
    touched.

    Clearing a claim (seq='') pops both overrides too: an override for a chord the
    action no longer holds is not a thing 'Applies in' has any way to show."""
    b = dict(bindings())
    w = dict(_when_over or {})
    ty = dict(_typing_over or {})
    if seq:
        b[cid] = seq
    else:
        b.pop(cid, None)
        w.pop(cid, None)
        ty.pop(cid, None)
    if when is not None:
        act = get(cid)
        if act is not None and act.owner == SHARED_OWNER:
            # The editor sends back the whole domain it was shown; the providers'
            # commands in it are not an override (see _declare_shared).
            when = {k: v for k, v in when.items() if k != 'command'}
        if when:
            w[cid] = when
        else:
            w.pop(cid, None)
    if typing is not None:
        if typing:
            ty[cid] = True
        else:
            ty.pop(cid, None)
    _write_doc(b, w, dict(_routes or {}), ty)
    return b


def _ensure_route_def(rid):
    """Re-register a route's ButtonDefinition after a reload, the same as
    _register_definition does for a declared action — Fusion keeps the command
    definition across a reload, but nothing in a freshly re-exec'd module has a
    live pointer to it, and it needs to exist before Fusion can hold a chord on
    it. No commandCreated handler: _on_command_starting cancels a route before
    Fusion ever gets far enough to create the command, so nothing would call it.
    Wrapped — _ui is a stub in offline tests, and this must not break the load
    just because Fusion is not actually there."""
    try:
        cd = _ui.commandDefinitions.itemById(rid)
        if not cd:
            _ui.commandDefinitions.addButtonDefinition(rid, rid, rid)
    except Exception:
        _log('could not register route', rid, traceback.format_exc().replace('\n', ' | '))


def ensure_route(seq):
    """Find-or-create the synthetic anchor command for `seq`, and record it in
    bindings.json's `routes` map. Called when a chord needs a route for the
    first time — see remap/fusion.py::apply_plan."""
    rid = route_id(seq)
    _ensure_route_def(rid)
    if _routes.get(rid) != seq:
        routes = dict(_routes or {})
        routes[rid] = seq
        _write_doc(dict(_bindings or {}), dict(_when_over or {}), routes)
    return rid


def sync_from_keymap(entries):
    """Adopt bindings made in Fusion's own keyboard panel.

    Fusion's keymap stays a legitimate front door: if a key was pointed at one of our
    actions there, that is the assignment, and it also tells us which claimant is the
    anchor for that sequence."""
    by_id = {}
    for e in entries or []:
        for c in e.get('commands', []):
            by_id[c['id']] = e['seq']
    changed = False
    for a in actions():
        seq = by_id.get(a.id, '')
        a.seq_hint = seq
        if a.adopted:
            continue      # an adopted id's binding IS the live keymap; nothing to sync
        if seq and binding_of(a.id) != seq:
            set_binding(a.id, seq)
            changed = True
    return changed


# ── Loader ─────────────────────────────────────────────────────────────────────
#
# Copy this into any add-in that declares actions. It MUST go through sys.modules:
# loading the file directly gives every add-in its own registry and its own router.
#
# It also re-execs this file when it changes: hotkeys.py is outside the AddIns folder,
# so Reload Recent AddIns cannot reach it, and the cache would otherwise pin the old
# engine until Fusion restarted. Re-exec into the SAME __dict__ is what the globals()
# recovery at the top of this file exists for.
#
#     import importlib.util, os, sys
#     HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
#     def _hotkeys():
#         mod = sys.modules.get('fusion_hotkeys')
#         try:    stamp = os.path.getmtime(HOTKEYS_PY)
#         except OSError: stamp = 0
#         if mod is None:
#             spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
#             mod = importlib.util.module_from_spec(spec)
#             sys.modules['fusion_hotkeys'] = mod
#             spec.loader.exec_module(mod)
#         elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
#             with open(HOTKEYS_PY) as f:
#                 exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
#         mod._engine_mtime = stamp
#         return mod
