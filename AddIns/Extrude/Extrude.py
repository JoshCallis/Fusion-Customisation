"""
Extrude — the native feature, wearing this window's face.

Fusion's own extrude is a good tool with a dialog that is in the way: eleven controls,
tooltips, an info button, and an extent-type dropdown you will use once a year. This is
the same feature, previewed the same way, with the panel from the value window in front
of it — and with three things Fusion's cannot know.

WHAT IT ADDS
────────────
  POSITIVE IS IN.  A sketch made by Smart Sketch is placed against a BOUND — "Width 100"
  is 100mm in from the side — and material added there goes inward, toward the opposite
  bound. Outward is off the end of the component. So the sketch's own datum line decides
  which way a positive distance goes, exactly, with nothing inferred; a sketch that has
  no line (or sits on a midline, which has no "in"), and a face or body, fall back to the
  nearest bound and the same reasoning. See Bounds.inward_vector.

  ITS OWN COMPONENT.  The feature is created in the component the profile belongs to,
  whether or not that is the active one — extruding a sketch you can see from a level
  above should not first require going down to it, and should certainly not drop the body
  wherever you happen to be standing.

  ITS OWN COMPONENT'S BODIES, AND NOTHING ELSE.  Participants are that component's own
  bodies. Not the root's, not a child's. A component is modelled against its own bounds
  and its own material (see component_self_containment_rule); a cut that reaches into a
  sibling is the kind of cross-component link this whole scheme exists to avoid. Of
  those, a new join, cut or intersect starts on the ones it actually runs into, hidden
  ones left out, and hidden material does not make the auto rule say cut.

  CLICK A BODY TO ADD IT.  While the command runs, a body clicked in the viewport or in
  the Browser window is put in or taken out of what the operation works on — never
  selected, never extruded (faces are not offered at all; extrusions come from sketches).
  A group clicked in the Browser flips all of its bodies together. The panel's Bodies
  row shows the list and has a Clear. See "Which bodies". With "Bodies are: Group" a
  click takes the body's whole Browser group, and the feature keeps following its
  groups once made (see "Following a group" — the Browser warns before exploding one).
  "Bodies are: Component" needs no click: every body of the component the extrusion
  lands in, and it keeps following the component's bodies the same way.

  EDITING GOES THROUGH HERE TOO.  Edit Feature on an extrude (timeline right-click or
  double-click) opens this panel on that feature instead of Fusion's dialog, and OK
  edits it in place. So does starting this command with an extrude selected. Extents
  the panel cannot express still get Fusion's dialog. See "Editing an existing extrude".

HOW IT IS A SKIN AND NOT A REIMPLEMENTATION
───────────────────────────────────────────
The command running is OURS, but everything it does is Fusion's. `executePreview` builds
a real ExtrudeFeature on every change, so what you see in the viewport is the actual
feature — the same engine, the same graphics — and OK keeps the last one rather than
computing anything new.

What could NOT be inherited, and is worth being plain about: the API's
ExtrudeFeatureInput has no "auto" operation and no auto-pick of participant bodies. That
inference lives only inside the native command's own dialog, and no add-in can reach it
or read a running native command's inputs. So `_auto_operation` below is our rule, not
Fusion's, written to land where Fusion lands: material ahead of the profile means cut,
material behind it means join, nothing either side means a new body.

THE PANEL IS THE VALUE WINDOW
─────────────────────────────
Distance, taper and the start offset are EXPRESSIONS, so they want the situational
autocomplete that already exists there — the same reason New Component and Smart Sketch
live in that window. Unlike those two, this one is not fire-and-forget: a command is
running behind the panel for as long as it is open, so the traffic goes both ways. The
panel posts every edit; this pushes the operation back each time Fusion's rules move it.

WHY IT NEEDS A QUEUE OF ITS OWN
───────────────────────────────
The Dimension Value add-in owns the window's server and drains its command queue
in-process. This add-in cannot: it has to be a separate add-in, because it needs its own
live Fusion command to hang the preview off, and a separate add-in is a separate module
object reachable only over HTTP. So the server keeps a second queue and this polls it —
see _extrude_queue there.
"""

import ctypes
import importlib.util
import json
import os
import sys
import threading
import time
import traceback
import types
import urllib.error
import urllib.request

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

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
    # Without it there is no "in", so the direction falls back to the profile's own
    # normal — which is Fusion's behaviour, and never a reason for the tool to fail.
    bounds = None

PLANE_GUESS_PY = '/Users/josh/Code/Scripts/fusion_ui/plane_guess.py'
try:
    plane_guess = types.ModuleType('plane_guess')
    plane_guess.__file__ = PLANE_GUESS_PY
    with open(PLANE_GUESS_PY, 'r', encoding='utf-8') as _f:
        exec(compile(_f.read(), PLANE_GUESS_PY, 'exec'), plane_guess.__dict__)
except Exception:
    plane_guess = None      # a reference pick then gives a plain number

# "Make parameter" from the panel — the command owns the parameters it makes, the way it
# owns its feature, or a preview's rollback takes them.
madeparams = _load('madeparams', 'Made Params.py')
# A typed distance/taper/offset becomes a hidden number parameter on execute (_value).
numbers = _load('numbers', 'Number Params.py')
_made = madeparams.Made()

try:
    # The Browser palette's Default for New Bodies. Worn inside the build, so a new
    # body's PREVIEW is already that colour rather than Fusion's stock white.
    defapp = _load('defapp', 'Default Appearance.py')
except Exception:
    defapp = None

# Browser groups (Groups.py, shared with Combine): "Bodies are: Group" — see "Which
# bodies" and "Following a group".
groups = _load('groups', 'Groups.py')
tickguard = _load('tickguard', 'Tick Guard.py')

CMD_ID = 'SmartExtrudeCmd'
CMD_NAME = 'Extrude'
CMD_DESC = ('Extrude the selected profile, face or body. A positive distance goes INTO '
            'the component, away from the bound the sketch was placed against.')
PANEL_ID = 'SolidScriptsAddinsPanel'

# ── Actions ───────────────────────────────────────────────────────────────────
# Things inside this command that want a key. Cycle Operation and Cycle Direction are
# the SHARED actions of fusion_ui/hotkeys.py — one key each for every command that has
# an operation or a direction — and this add-in PROVIDES what they mean while an
# extrude is running.
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'

# What "next" means. The panel's own order, so the key walks the row you are looking at
# rather than a second, private order. 'auto' is not on the row or in the ring: it is
# only what the command STARTS on, shown as the operation the rule lands on, and the
# first choice — a click or this key — replaces it for good.
_OPERATION_CYCLE = ('cut', 'intersect', 'join', 'newbody')

# The same for the direction row, in the order the panel draws it.
_DIRECTION_CYCLE = ('one', 'symmetric', 'two')


def _hotkeys():
    """The shared action registry, via sys.modules: ONE registry and ONE router for
    every add-in. Loading the file directly would give each add-in a private copy and
    the whole thing would quietly stop routing.

    It also hot-reloads itself. hotkeys.py lives outside the AddIns folder, so Reload
    Recent AddIns cannot map it to its consumers, and the sys.modules cache would
    otherwise pin the old engine until Fusion restarted. Re-exec is into the SAME
    __dict__, which is exactly what hotkeys.py's globals() recovery is built for — the
    registry and the live commandStarting registration both survive it.
    """
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod      # before exec: the module may re-enter
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod


def _step(ring, current, fallback='', d=1):
    """The entry `d` on from `current` in `ring` (-1: the one before), or its first
    entry when `current` is not in it at all."""
    try:
        return ring[(ring.index(current) + d) % len(ring)]
    except ValueError:
        return fallback or ring[0]


def _cycle_operation(d=1):
    """Step the operation on one, and redraw.

    THE STEP IS FROM WHAT IT IS DOING. The command starts on 'auto', which is not an
    operation — it is a deferral to the rule, which has already landed on join, cut or a
    new body and is showing you that as the selected one. So the first press moves on
    from THAT: an extrude that is cutting goes to intersect. Pressing the key means "not
    this one", and "this one" is whatever the row is currently showing.

    Runs from the router INSIDE the live extrude: the command that carried the key is
    vetoed before Fusion can start it, so the extrude is not terminated and the preview
    it is holding stays valid. Nothing here touches the command system.
    """
    if not _running or _cmd is None:
        return
    current = _spec.get('operation', 'auto')
    # On 'auto', step from the operation the rule landed on — the last preview's, or,
    # with no preview yet (no distance typed), the one the panel opened showing.
    start = current
    if current == 'auto':
        design = adsk.fusion.Design.cast(app.activeProduct)
        start = (_resolved.get('operation')
                 or (_auto_word(design) if design is not None else '') or 'newbody')
    nxt = _step(_OPERATION_CYCLE, start, d=d)
    _spec['operation'] = nxt
    _log('cycle operation', current, '(doing', start + ') ->', nxt)
    _defer_choose()


def _cycle_direction(d=1):
    """Step one side / two sides / symmetric on, and redraw. Same reasoning as
    _cycle_operation, minus the auto case — a direction is always exactly what it
    says."""
    if not _running or _cmd is None:
        return
    current = _spec.get('direction', 'one')
    nxt = _step(_DIRECTION_CYCLE, current, d=d)
    _spec['direction'] = nxt
    _log('cycle direction', current, '->', nxt)
    _defer_choose()


def _defer_choose():
    """The row first, the viewport after. Push the new word now and leave the preview
    (or apply) to a tick that runs once the poller has sent it: a preview holds the main
    thread — and with it the poller — for as long as it computes, so previewing here
    meant the panel heard about the key only when the viewport already showed it.

    Only for a key Fusion had. A key pressed in the panel never comes here: the panel
    steps the row itself and posts the choice (index.js, cycleKeys)."""
    _push_state()
    with _box_lock:
        _choose_after[0] = (_queued[0], time.time() + 0.5)


def _run_deferred_choose():
    """The tick's half of _defer_choose. Sent is not yet SHOWN: the server's thread still
    has to answer the panel's long-poll, and it cannot while a preview holds the main
    thread — hence the beat after the send. The half-second is for a poller that never
    gets there (the window down): the preview still has to happen."""
    pending = _choose_after[0]
    if pending is None:
        return
    need, give_up = pending
    sent = _sent[0] >= need and time.time() - _sent_at[0] >= 0.04
    if not sent and time.time() < give_up:
        return
    _choose_after[0] = None
    _choose()
    _push_state()       # in case there was nothing to preview yet


def _declare_actions():
    hk = _hotkeys()
    hk.provide('operation', 'Extrude', {'command': 'extrude'}, _cycle_operation)
    hk.provide('direction', 'Extrude', {'command': 'extrude'}, _cycle_direction)
    hk.provide('operation_back', 'Extrude', {'command': 'extrude'},
               lambda: _cycle_operation(-1))
    hk.provide('direction_back', 'Extrude', {'command': 'extrude'},
               lambda: _cycle_direction(-1))
    # The command itself, declared as a claimant so it can SHARE a key with the cycles
    # above: starting an extrude and cycling its operation are mutually exclusive on the
    # 'command' axis, so one key can be both. Native — Fusion runs it, we only stand
    # aside for it.
    hk.declare(CMD_ID, CMD_NAME, CMD_DESC, owner='Extrude',
               when={'command': 'none'}, native=True)
    hk.attach()


VALUE_URL = 'http://127.0.0.1:8734'

# This runs while a command is active, which is exactly when a message box would be an
# outrage and when the MCP bridge refuses to run. So it writes a line instead — the same
# thing Smart Dimension does, and for the same reason.
LOG = '/tmp/fusion_extrude.log'

# How often the tick asks the server what the panel has changed. The panel is a person
# typing, so this is a latency budget, not a data rate.
_TICK_SECS = 0.06
_EVENT_ID = 'SmartExtrudeTick'

# Registered handlers, kept so stop() can take them off their events again. Clearing the
# list without unregistering frees the object while Fusion still holds a pointer to it,
# and the command then silently does nothing at all — no error, no dialog.
_created = None
_custom_evt = None
_sync = None
_poller_stop = None

# The live command, and the pieces of it the tick needs. All None whenever no extrude is
# running, which is what every entry point here checks first.
_cmd = None
_sel_input = None
_running = False

# What the panel says the extrude should be. Expressions are kept as TYPED, never as
# evaluated numbers: what goes into the feature is the text, so that a distance of
# "MembraneThickness" stays linked to the parameter instead of being frozen at its value.
_DEFAULT_SPEC = {
    'distance': '',
    # 'distance' (the field is a length) | 'to' (the field is a DATUM LINE — "Top 5",
    # "Top - BootCeiling_Inset_Top" — and the extrude runs from the profile to that plane;
    # see _to_distance). The line is kept in a feature attribute so an edit reads it back.
    'reach': 'distance',
    # {name, flexible, min, max} for a distance that is one user parameter: written onto
    # that parameter at execute (Section Sketch's flex — see _write_flex).
    'flex': None,
    'distance2': '',      # the far side, when direction is 'two'
    'direction': 'one',   # 'one' | 'two' | 'symmetric'
    'operation': 'auto',  # 'auto' | 'join' | 'cut' | 'intersect' | 'newbody'
    'isSolid': True,      # advanced: type — a solid, or a surface
    'start': 'profile',   # advanced: 'profile' | 'offset'
    'startOffset': '',
    'taper': '',          # advanced
    # Bodies a join, cut or intersect must LEAVE ALONE, by _body_keys key. Kept as what
    # is left out rather than what is in, so a body made after this was set (a re-pick, a
    # different preview) joins in by default the way it would natively.
    # Only ever replaced, never mutated — dict(_DEFAULT_SPEC) shares this list.
    'exclude': [],
    # While True, every build rewrites `exclude` as "whatever the extrusion does not
    # reach" (see _auto_exclude) — the bodies it runs into are in, the rest out, and that
    # follows the distance as you type it. The first body you click (viewport, Browser,
    # a chip on the panel, the panel's Clear) makes the list yours and stops that.
    'bodiesAuto': True,
    # 'bodies' | 'group'. Group: a join, cut or intersect works on every body (of its
    # component) in the Browser groups `groups` — a click takes a body's whole group in
    # or out, `exclude` is worked out from them, and the feature keeps following them
    # after it is made (_GROUPS_ATTR). While the list is still automatic, the groups are
    # those of the bodies the extrusion runs into. 'component': every body of the
    # component, followed the same way.
    'bodiesScope': 'bodies',
    'groups': [],          # group ids; only ever replaced, like exclude
    # Off: nothing is computed while you work. No viewport preview; on an EDIT a
    # choice (direction, operation, type, start) is applied to the feature for real the
    # moment it is clicked, and typed values go in on Enter. On by default, and switched
    # off before the panel opens for an edit of a feature too heavy to preview — see
    # _too_heavy. The panel's Preview row toggles it.
    'preview': True,
}
# There is no "flip". A negative distance reverses the extrusion, which is one keystroke
# and needs no control — and once positive means INTO the component, a minus sign reads
# as "outward" rather than as an arbitrary sign convention. A button would be a second
# way to say the same thing.
_spec = dict(_DEFAULT_SPEC)

# The newest `rev` the panel has sent that this side has acted on, echoed on every push.
# The panel moves a row the moment it is clicked and holds it against pushes until one
# carries a rev at least as new as the click — a push that left before the click landed
# (the end of a slow preview, say) still says the old word, and a timer could not tell
# the two apart: a preview longer than the timer flicked the row back. See index.js,
# "What we have asked for".
_rev = [0]

# The panel's expression fields.
_EXPR_FIELDS = ('distance', 'distance2', 'startOffset', 'taper')

# Each field's last text that was a whole expression. A field half typed ("Width +",
# "Wid") previews as this instead, so the preview holds still until the text is viable
# again rather than vanishing on every keystroke. Dropped when the field is emptied: an
# empty distance really is "no preview", and a half-typed one after it is not a reason to
# bring back what was there before. See _viable_spec.
_last_good = {}

# What the standing preview was built from (_build_key), or None. A panel edit that comes
# to the same thing — another keystroke into a half-typed field — leaves it standing
# instead of rebuilding the identical feature.
_built_key = [None]

# The profiles being extruded are un-highlighted while a preview stands: the preview IS
# what they will become, and the selection's blue over it hid the result. Their
# selection comes back the moment there is no preview (distance emptied, preview off, a
# build that fails). _picked is what is extruded either way — the visible selection is
# only a picture of it. See _sync_highlight.
_sel_hidden = False
# Until when an inputChanged is our own selection write being reported back.
_sel_quiet_until = [0.0]

# A cycle key pressed while Fusion had the keyboard: the row's new word goes to the panel
# first, and the preview waits until the poller has sent it (_sent reaches this) — or
# the row would only move after the preview had computed. See _defer_choose.
_choose_after = [None]
_sent = [0]
_sent_at = [0.0]
_queued = [0]

# What the last preview actually did, pushed back to the panel so it shows the operation
# Fusion's rules landed on rather than the word "auto".
_resolved = {}

# What is being extruded. Seeded from the pre-selection at commandCreated, because
# Fusion clears the ordinary selection as a command starts and the pre-select workflow is
# the main one; then replaced whenever the viewport selection becomes something else.
# Never emptied by an empty selection — that would throw the pre-selection away the
# instant the command began.
_picked = []
_picked_id = ()

# The handlers belonging to the command currently running. Kept alive only for as long
# as that command is: Fusion holds pointers to them, so they cannot simply be dropped
# while it runs, but they must not accumulate across runs either.
_cmd_handlers = []

# Set while _cancel is ending the command, so the execute event knows to build nothing.
_cancelling = False

# Did a preview succeed and leave a valid feature standing? See _Execute — this is what
# stops OK building a second one on top of it.
_previewed = False

# Previews run since this command was created. Fusion fires one of its own before
# activate, so activate only asks for one when that has not happened.
_preview_runs = 0

# Set while _apply is running execute without ending the command; execute leaves what
# it built in _applied (None: it did not build, and the transaction is aborted).
_applying = False
_applied = None

# Seconds the last real build of an edit took, by feature token — what _too_heavy
# learns from, so a feature that turned out slow opens with preview off next time.
_build_secs = {}
# An edit opens with preview OFF when its feature has more faces than this (the cost of
# a preview is mostly redrawing them), or when its last build took longer than this.
_HEAVY_FACES = 250
_HEAVY_SECS = 1.5

# sys.modules key the Browser add-in calls through: a sketch clicked in the Browser
# window while this command is open means "extrude all of its profiles" — see
# _pick_sketches. Clicking one to measure a distance off it is never the intent. A body
# (or a group of them) clicked there goes in or out of what the operation works on —
# _pick_bodies — and `state` tells the Browser which bodies those are, to mark them.
# Click Actions asks `running` so a body click here never also activates anything.
HOOK = 'fusion_extrude_hook'


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ── Talking to the window's server ───────────────────────────────────────────

def _post(path, payload):
    """Best effort, and deliberately so: with the window down there is nothing to steer
    the command from, but the command itself is still perfectly valid."""
    try:
        req = urllib.request.Request(
            VALUE_URL + path,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def _get(path, timeout=2):
    try:
        with urllib.request.urlopen(VALUE_URL + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# Nothing on Fusion's main thread is allowed to touch the network. A blocking HTTP call
# per tick was the first shape this took and it was wrong twice over: 60ms of latency
# lands on the thread that draws the viewport, and if a call ever ran long the custom
# events queued behind it without bound. So the poller thread does ALL of the I/O and
# these two boxes carry the results across.
_inbox = []            # commands fetched by the poller, drained by the tick
_outbox = []           # (path, payload) posted by the tick, sent by the poller
_box_lock = threading.Lock()


def _send(path, payload):
    """Queue a post for the poller thread. Called from the main thread."""
    with _box_lock:
        _outbox.append((path, payload))
        _queued[0] += 1


# ── Reading what is being extruded ───────────────────────────────────────────

def _extrudable(entity):
    """Is this something an extrude is built FROM? Sketch profiles only. Everything else
    that is clicked — a face, an edge, a vertex, a sketch line or point, a plane — is a
    REFERENCE to extrude TO (_adopt_reference). The command has no visible dialog to
    filter picks for it, so it filters them here."""
    try:
        return adsk.fusion.Profile.cast(entity) is not None
    except Exception:
        return False


def _viewport_picks():
    """The extrudable things currently selected in the viewport."""
    out = []
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            ent = sel.item(i).entity
            if _extrudable(ent):
                out.append(ent)
    except Exception:
        pass
    return out


def _identity(entities):
    """Something comparable across ticks, so a selection can be diffed."""
    out = []
    for e in entities:
        try:
            out.append(e.entityToken)
        except Exception:
            out.append(repr(e))
    return tuple(out)


def _selected_entities():
    """What the extrude is being built FROM.

    Read from ui.activeSelections, NOT from the command's own SelectionCommandInput —
    which is the opposite of how a Fusion command normally works, and is forced by the
    hidden palette. A selection input only receives picks while it has focus, and focus
    is something you give it by clicking it in the dialog. With the dialog hidden nothing
    ever did, so every click went to the ordinary selection instead: the face lit up blue
    exactly as it should and the panel never heard about it, which is what "it doesn't
    recognise faces or profiles I pick" was. Setting hasFocus explicitly does not fix it
    (it is accepted and has no effect) — measured, not assumed.

    ui.activeSelections is readable while a command runs; this codebase already leans on
    that in Smart Dimension. See _picked for how the pre-selection is kept.
    """
    return list(_picked)


def _native(entity):
    """The definition behind a proxy, or the entity itself.

    Everything downstream — which component owns it, which bodies are its siblings —
    is a question about the definition, not about the instance you happened to click.
    """
    try:
        return entity.nativeObject or entity
    except Exception:
        return entity


def _sketch_of(entity):
    """The sketch a profile belongs to, or None for anything else."""
    prof = adsk.fusion.Profile.cast(entity)
    if prof is None:
        return None
    try:
        return prof.parentSketch
    except Exception:
        return None


def _component_of(entity):
    """The component the extrusion belongs in: the one that owns what it is made FROM.

    Not the active component. Extruding a sketch you can see from a level above should
    not first require descending to it, and should certainly not drop the new body
    wherever you happen to be standing.
    """
    ent = _native(entity)
    sketch = _sketch_of(ent)
    if sketch is not None:
        try:
            return _native(sketch).parentComponent
        except Exception:
            return None
    for attr in ('parentComponent', 'body'):
        try:
            got = getattr(ent, attr, None)
            if got is None:
                continue
            if attr == 'body':
                return _native(got).parentComponent
            return got
        except Exception:
            continue
    return None


def _plane_of(entity):
    """(normal, point) in model space for whatever is being extruded, or (None, None)."""
    ent = _native(entity)
    sketch = _sketch_of(ent)
    if sketch is not None:
        try:
            origin, _xa, _ya, za = sketch.transform.getAsCoordinateSystem()
            return za, origin
        except Exception:
            return None, None
    face = adsk.fusion.BRepFace.cast(ent)
    if face is not None:
        try:
            plane = adsk.core.Plane.cast(face.geometry)
            if plane is not None:
                return plane.normal, plane.origin
        except Exception:
            pass
    return None, None


def _profile_point(entity):
    """A point in the middle of what is being extruded, in the COMPONENT's own space, or
    None.

    Component space, not world, because that is the space the bodies it will be tested
    against are in. For everything here the two coincide only when the occurrence sits at
    the identity, which is exactly the case that hides the bug.

    A profile's centroid arrives in SKETCH space — measured, not assumed: for a sketch
    2.5cm up the component's Z it still comes back at z=0 — so it has to be carried
    through sketch.transform. Without that every probe landed near the component origin,
    found nothing there, and every extrude in the world came out "New Body".

    Used only to ask which side of the profile has material on it. The centroid is not
    guaranteed to lie inside a re-entrant profile, which is a known limit of the operation
    guess and not of anything the guess is then used for — the worst case is that the
    panel opens on "New Body" and you pick the operation yourself.
    """
    ent = _native(entity)
    prof = adsk.fusion.Profile.cast(ent)
    if prof is not None:
        try:
            props = prof.areaProperties(
                adsk.fusion.CalculationAccuracy.LowCalculationAccuracy)
            point = props.centroid
            sketch = _sketch_of(ent)
            if sketch is not None:
                point = point.copy()
                point.transformBy(sketch.transform)
            return point
        except Exception:
            return None
    face = adsk.fusion.BRepFace.cast(ent)
    if face is not None:
        # Already in its body's space, which is the component's.
        try:
            return face.pointOnFace
        except Exception:
            return None
    return None


def _inward(design, comp, entity):
    """A unit vector pointing INTO `comp` from the plane being extruded, or None.

    None means the rule does not apply — the plane is not square to a principal axis,
    the component has no bounds on that axis, or it sits dead centre between the two —
    and the caller leaves Fusion's own default direction alone rather than flip a coin.
    """
    if bounds is None or comp is None:
        return None
    try:
        sketch = _sketch_of(_native(entity))
        normal, point = _plane_of(entity)
        return bounds.inward_vector(design, comp, sketch=sketch,
                                    normal=normal, point=point)
    except Exception:
        _log('inward failed:', traceback.format_exc().replace('\n', ' | '))
        return None


# ── Which operation ──────────────────────────────────────────────────────────
# The one thing that could NOT be inherited. Fusion's own extrude picks join, cut or new
# body from what the extrusion would run into, but that inference lives inside its
# dialog: ExtrudeFeatureInput has no "auto", and a running native command's inputs are
# not readable from an add-in. So this is our rule, written to land where Fusion's lands.
#
#   material AHEAD of the profile   → cut     (you are going into something)
#   material BEHIND it              → join    (you are on a face, coming off it)
#   neither                         → new body
#
# Candidates are the component's OWN bodies and nothing else: not the root's, not a
# child's. See the module docstring.

# How far past the profile to look for material. 0.1mm in Fusion's internal centimetres:
# far enough inside to clear the surface it is testing against, far short of any real
# feature.
_PROBE = 0.01


def _own_bodies(comp):
    """The component's own solid bodies. A list, so it can be handed to participantBodies
    and counted without re-walking the collection."""
    out = []
    if comp is None:
        return out
    try:
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            if body.isSolid:
                out.append(body)
    except Exception:
        pass
    return out


def _hidden(body):
    """Switched off in the browser. A body you cannot see is not one you mean to cut."""
    try:
        return not body.isVisible
    except Exception:
        return False


def _body_keys(bodies):
    """A key per body, in order: its name, or "name (2)" for a second of the same name.

    Names rather than entity tokens because the panel shows them and because they survive
    what every preview does to the model — a token taken before one preview is not
    reliably the token the same body answers to after it."""
    seen = {}
    keys = []
    for body in bodies:
        try:
            name = body.name
        except Exception:
            name = 'Body'
        seen[name] = seen.get(name, 0) + 1
        keys.append(name if seen[name] == 1 else '%s (%d)' % (name, seen[name]))
    return keys


def _seed_participants(feature, bodies):
    """On the first build of an edit, read which bodies the feature already cuts into
    `exclude`. Here rather than in _edit_plan because the getter wants the timeline
    rolled to just before the feature, which _build has done and commandStarting must
    not. Empty participantBodies means "all of them", which is an empty exclude."""
    if _edit.get('bodiesRead'):
        return
    _edit['bodiesRead'] = True
    try:
        chosen = list(feature.participantBodies or [])
    except Exception:
        _log('participantBodies unreadable:', traceback.format_exc().replace('\n', ' | '))
        return
    if not chosen:
        return
    exclude = [k for body, k in zip(bodies, _body_keys(bodies))
               if not any(body == c for c in chosen)]
    _spec['exclude'] = exclude
    _edit.setdefault('seed', {})['exclude'] = list(exclude)


# ── Which bodies ─────────────────────────────────────────────────────────────
# Fusion's own extrude starts a join or cut on the bodies it runs into and lets you click
# bodies on and off from there. Same here: _auto_exclude is the "runs into" half, and
# _toggle_bodies is the clicking — from the viewport (the tick, _adopt_body_clicks) or
# the Browser window (HOOK.pick_bodies). A body is never SELECTED by any of this.

# How close counts as touching, in Fusion's centimetres: a join off a face shares that
# face exactly, and a bounding box can come back a hair short of it.
_TOUCH = 1e-4


def _boxes_meet(a, b):
    try:
        a, b = a.boundingBox, b.boundingBox
        return (a.minPoint.x <= b.maxPoint.x + _TOUCH and b.minPoint.x <= a.maxPoint.x + _TOUCH
                and a.minPoint.y <= b.maxPoint.y + _TOUCH and b.minPoint.y <= a.maxPoint.y + _TOUCH
                and a.minPoint.z <= b.maxPoint.z + _TOUCH and b.minPoint.z <= a.maxPoint.z + _TOUCH)
    except Exception:
        return True


def _reaches(tool, body, operation):
    """Does the swept volume `tool` run into `body`? For a cut or intersect that means
    shares volume with it; for a join, touches it at all (a boss off a face shares no
    volume, only the face). Temporary copies, so the model is never touched. Anything the
    geometry kernel refuses answers True — a body wrongly left in is visible on the
    panel, one wrongly left out is a cut that silently skips it."""
    if not _boxes_meet(tool, body):
        return False
    tbm = adsk.fusion.TemporaryBRepManager.get()
    try:
        target = tbm.copy(body)
        before = target.lumps.count
        if operation == adsk.fusion.FeatureOperations.JoinFeatureOperation:
            tbm.booleanOperation(target, tbm.copy(tool),
                                 adsk.fusion.BooleanTypes.UnionBooleanType)
            return target.lumps.count < before + tool.lumps.count
        tbm.booleanOperation(target, tbm.copy(tool),
                             adsk.fusion.BooleanTypes.IntersectionBooleanType)
        return target.faces.count > 0 and target.volume > 1e-9
    except Exception:
        _log('reach test failed:', traceback.format_exc().replace('\n', ' | '))
        return True


def _auto_exclude(own, keys, tool, operation):
    """The bodies to leave out while the list is automatic: hidden ones (a body you cannot
    see is not one you mean to cut) and every one the extrusion does not reach. Without a
    tool body to test against — it did not build — only the hidden ones, which is what
    the whole component minus the invisible used to mean."""
    return [k for b, k in zip(own, keys)
            if _hidden(b) or (tool is not None and not _reaches(tool, b, operation))]


def _body_key_in(own, keys, body):
    """The _body_keys key of `body` among `own`, or None when it is not one of them.

    By identity first and by name second: a body clicked in the viewport while a preview
    stands is the PREVIEW's copy of it, which is not == to the one the rolled-back model
    holds, and carries the same name."""
    native = _native(body)
    for b, k in zip(own, keys):
        try:
            if b == native:
                return k
        except Exception:
            continue
    try:
        name = native.name
    except Exception:
        return None
    return name if name in keys else None


def _groups_of(design, comp, bodies):
    """The ids of the groups these bodies of `comp` are filed in, in body order."""
    folders, leaves = groups._doc_groups(design)
    out = []
    for k, b in groups.keyed_bodies(comp):
        g = groups.top(folders, leaves.get(k))
        if g in folders and g not in out and any(b == x for x in bodies):
            out.append(g)
    return out


def _group_keys(design):
    """Every leaf key in the chosen groups."""
    out = set()
    for g in _spec.get('groups') or []:
        out |= groups.member_keys(design, g) or set()
    return out


def _group_chips(design, comp):
    """[[id, name]] for the panel's group chips: every group one of the component's
    bodies is filed in, then any chosen group that isn't one of those."""
    ids = _groups_of(design, comp, [b for _, b in groups.keyed_bodies(comp)])
    ids += [g for g in _spec.get('groups') or [] if g not in ids]
    return [[g, groups.group_name(design, g)] for g in ids]


def _panel_group_chips(design):
    """The chips for the panel with "Bodies are: Group", from the pick's component —
    not from the last build, so they are there before a distance is typed."""
    if design is None or _spec.get('bodiesScope') != 'group':
        return []
    entities = _selected_entities()
    comp = _component_of(entities[0]) if entities else None
    return _group_chips(design, comp) if comp is not None else []


def _toggle_groups(comp, bodies, group=None):
    """_toggle_bodies with "Bodies are: Group": the clicked bodies' groups (or the group
    row clicked in the Browser) go in or out as one unit."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if group:
        gids = [group]
    else:
        gids = []
        for b in bodies:
            g = groups.group_of(design, b)
            if g is None:
                _send('/extrude/error', {'error': '%s is not in a group.' % _native(b).name})
                return False
            if g not in gids:
                gids.append(g)
    own = {k for k, _ in groups.keyed_bodies(comp)}
    gids = [g for g in gids if (groups.member_keys(design, g) or set()) & own]
    if not gids:
        _send('/extrude/error', {'error': 'That group has no bodies in %s.' % comp.name})
        return False
    cur = list(_spec.get('groups') or [])
    if all(g in cur for g in gids):
        cur = [g for g in cur if g not in gids]
    else:
        cur += [g for g in gids if g not in cur]
    _spec['groups'] = cur
    _spec['bodiesAuto'] = False
    _preview()
    return True


def _seed_groups():
    """"Bodies are: Group" chosen after the list was made yours: the groups of the bodies
    taking part now. (Still automatic, the next build works them out itself.)"""
    if _spec.get('bodiesAuto', True):
        return
    entities = _selected_entities()
    comp = _component_of(entities[0]) if entities else None
    design = adsk.fusion.Design.cast(app.activeProduct)
    if comp is None or design is None:
        return
    own = _own_bodies(comp)
    out = set(_spec.get('exclude') or [])
    _spec['groups'] = _groups_of(design, comp,
                                 [b for b, k in zip(own, _body_keys(own)) if k not in out])


def _toggle_bodies(bodies, group=None):
    """Put these bodies in, or take them out, as ONE unit: if every one of them already
    takes part they all come out, otherwise they all go in. One body is a toggle; a group
    from the Browser flips whole. Bodies outside the component the extrusion lands in
    are ignored — see the module docstring on why a feature never reaches outside its own
    component. Main thread. False when nothing changed."""
    entities = _selected_entities()
    comp = _component_of(entities[0]) if entities else None
    if comp is None:
        _send('/extrude/error', {'error': 'Pick a profile first, then the bodies.'})
        return False
    if _spec.get('bodiesScope') == 'group':
        return _toggle_groups(comp, bodies, group)
    if _spec.get('bodiesScope') == 'component':
        _send('/extrude/error', {'error': 'Bodies are: Component — every body of %s '
                                          'takes part.' % comp.name})
        return False
    own = _own_bodies(comp)
    keys = _body_keys(own)
    mine = []
    foreign = 0
    for body in bodies:
        k = _body_key_in(own, keys, body)
        if k is None:
            foreign += 1
        elif k not in mine:
            mine.append(k)
    if not mine:
        if foreign:
            _send('/extrude/error', {'error': 'Not a body of %s.' % comp.name})
        return False
    out = set(_spec.get('exclude') or [])
    # Nothing built here yet (no distance typed): there is no automatic list to edit, so
    # a click starts from none rather than taking a body out of "all of them".
    if _spec.get('bodiesAuto', True) and _resolved.get('component') != comp.name:
        out = set(keys)
    if any(k in out for k in mine):
        out.difference_update(mine)
    else:
        out.update(mine)
    _spec['exclude'] = [k for k in keys if k in out]
    _spec['bodiesAuto'] = False
    _preview()
    return True


def _clear_body_picks(sel):
    """Take every body back out of Fusion's selection, keeping the profiles — a body
    click toggles, it never selects. Rebuilt rather than removed one by one: clear + add
    is what _pick_sketches already does to this selection under a running command."""
    keep = []
    for i in range(sel.count):
        ent = sel.item(i).entity
        if adsk.fusion.BRepBody.cast(ent) is None:
            keep.append(ent)
    sel.clear()
    for ent in keep:
        try:
            sel.add(ent)
        except Exception:
            pass


def _adopt_body_clicks():
    """A body clicked in the viewport since the last tick. Main thread, on the tick."""
    try:
        sel = ui.activeSelections
        bodies = [sel.item(i).entity for i in range(sel.count)
                  if adsk.fusion.BRepBody.cast(sel.item(i).entity) is not None]
    except Exception:
        return
    if not bodies:
        return
    _clear_body_picks(sel)
    # Each click is its own toggle, however many landed in one tick.
    for body in bodies:
        _toggle_bodies([body])


def _pick_bodies(bodies, group=None):
    """Bodies clicked in the Browser window while this command is open — one row, or
    every body in a group (`group`, its id, when the row was a group). Called through
    HOOK, on the main thread. False when no extrude is running, so the Browser does what
    it normally does."""
    if not _running or _cmd is None:
        return False
    try:
        _toggle_bodies(bodies, group)
    except Exception:
        _log('pick bodies failed:', traceback.format_exc().replace('\n', ' | '))
    return True


def _hook_state():
    """For the Browser window: is an extrude open, and which bodies take part — as the
    Browser's own durable row keys ('<component id>|bd|<name>'), so it can mark them."""
    if not _running or _cmd is None:
        return {'on': False, 'keys': []}
    comp_id = _resolved.get('componentId', '')
    op = _resolved.get('operation', '')
    if op not in ('join', 'cut', 'intersect'):
        return {'on': True, 'keys': []}
    out = set(_spec.get('exclude') or [])
    try:
        comp = adsk.fusion.Design.cast(app.activeProduct).allComponents.itemById(comp_id)
        own = _own_bodies(comp)
        taking = [b for b, k in zip(own, _body_keys(own)) if k not in out]
        # The Browser's key (Visibility Snapshot's _leaf_key): a repeat of a name is
        # '#n' by its place among ALL the component's same-named bodies — not this
        # panel's "name (2)", which counts solids only. A body stamped with an id
        # (Visibility Snapshot's stamp_bodies) is '<cid>|bd|@<id>' instead, the first
        # holder of an id keeping it.
        keys, seen, uids = [], {}, set()
        for i in range(comp.bRepBodies.count):
            b = comp.bRepBodies.item(i)
            name = b.name
            n = seen[name] = seen.get(name, 0) + 1
            try:
                a = b.attributes.itemByName('Browser', 'uid')
                uid = (a.value or None) if a else None
            except Exception:
                uid = None
            if uid and uid not in uids:
                uids.add(uid)
                key = '%s|bd|@%s' % (comp_id, uid)
            else:
                key = '%s|bd|%s%s' % (comp_id, name, '' if n == 1 else '#%d' % n)
            if any(b == t for t in taking):
                keys.append(key)
    except Exception:
        keys = []
    return {'on': True, 'keys': keys}


def _material_at(bodies, point):
    """Is `point` inside any of them?"""
    inside = adsk.fusion.PointContainment.PointInsidePointContainment
    for body in bodies:
        try:
            if body.pointContainment(point) == inside:
                return True
        except Exception:
            continue
    return False


def _auto_operation(comp, entity, direction):
    """What Fusion would most likely choose. `direction` is the unit vector the extrusion
    actually travels along. Hidden bodies are not material here: they start left out of
    a cut (see _auto_exclude), so a cut they alone called for would cut nothing."""
    bodies = [b for b in _own_bodies(comp) if not _hidden(b)]
    if not bodies:
        return adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    point = _profile_point(entity)
    if point is None or direction is None:
        # Nothing to reason from. A new body is the answer that cannot damage anything
        # that is already there.
        return adsk.fusion.FeatureOperations.NewBodyFeatureOperation

    def probe(sign):
        return adsk.core.Point3D.create(
            point.x + direction.x * _PROBE * sign,
            point.y + direction.y * _PROBE * sign,
            point.z + direction.z * _PROBE * sign)

    if _material_at(bodies, probe(+1)):
        return adsk.fusion.FeatureOperations.CutFeatureOperation
    if _material_at(bodies, probe(-1)):
        return adsk.fusion.FeatureOperations.JoinFeatureOperation
    return adsk.fusion.FeatureOperations.NewBodyFeatureOperation


_OPERATION_WORD = {
    'join': adsk.fusion.FeatureOperations.JoinFeatureOperation,
    'cut': adsk.fusion.FeatureOperations.CutFeatureOperation,
    'intersect': adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    'newbody': adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
}
_WORD_OPERATION = {v: k for k, v in _OPERATION_WORD.items()}


def _travel(normal, positive_is_inward):
    """The unit vector the extrusion actually travels along, from _orientation's answer —
    what the operation rule needs to know which side to look for material on."""
    if normal is None:
        return None
    try:
        travel = normal.copy()
        if not positive_is_inward:
            travel.scaleBy(-1)
        return travel
    except Exception:
        return None


def _auto_word(design):
    """The operation the rule lands on for what is picked now, as the panel's word, or ''
    with nothing picked. Needs no distance — the rule only looks either side of the
    profile — so the panel can open with it already selected, before anything has
    been typed or previewed. A component with no bodies yet (its first extrude) is
    always 'newbody'."""
    entities = _selected_entities()
    if not entities:
        return ''
    entity = entities[0]
    comp = _component_of(entity)
    if comp is None:
        return ''
    try:
        _inw, normal, positive_is_inward = _orientation(design, comp, entity)
        return _WORD_OPERATION.get(
            _auto_operation(comp, entity, _travel(normal, positive_is_inward)), 'newbody')
    except Exception:
        _log('auto operation failed:', traceback.format_exc().replace('\n', ' | '))
        return ''


# ── Building the feature ─────────────────────────────────────────────────────

# ── The ghost ────────────────────────────────────────────────────────────────
# A cut or an intersect shows you a body with less of it than before, and "less" is hard
# to see: the interesting thing about the operation is the volume that is GOING, and that
# volume is exactly what the finished model no longer contains. So it is drawn separately,
# as translucent custom graphics sitting where the material used to be — the same idea
# as Fusion's own cut preview.
#
# Only for cut and intersect. A join or a new body IS the volume, drawn solid and in the
# right place already; ghosting it too would just fog the thing you are looking at.
#
# In the operation's own colour — the one its button wears on the panel (fusion_ui/
# theme.css --op-*): cut red, intersect orange-yellow.

_GHOST_ID = 'SmartExtrudeGhost'
# Phong terms rather than one flat colour — see _draw_ghost for why that is the whole
# difference between a translucent volume and an opaque slab. (diffuse, ambient) per op.
_GHOST_COLOURS = {
    'cut': ((255, 60, 60, 255), (120, 20, 20, 255)),
    'intersect': ((255, 175, 30, 255), (120, 80, 10, 255)),
}
_GHOST_SPECULAR = (255, 255, 255, 255)
_GHOST_EMISSIVE = (0, 0, 0, 255)
_GHOST_GLOSS = 16.0
_GHOST_OPACITY = 0.35

# The group currently on screen, so it can be taken off again. Custom graphics are NOT
# part of the parametric model, so nothing rolls them back for us — every preview must
# clear the last one or they stack up, and the command must clear the last one on its way
# out or it is left painted over the model for good.
_ghost = None


def _clear_ghost():
    global _ghost
    if _ghost is None:
        return
    try:
        _ghost.deleteMe()
    except Exception:
        pass
    _ghost = None


def _tool_body(comp, subject, mode, side, taper, distance, start=None):
    """A temporary body of the volume the extrusion sweeps, or None.

    Built by extruding as a NEW BODY, copying the result, and deleting the feature again.
    Long-winded, and it is the only way: the tool volume is not something Fusion hands
    you, and once the real cut has run it is precisely what no longer exists. Used for
    the ghost and for which bodies the extrusion reaches (_auto_exclude).
    """
    feats = comp.features.extrudeFeatures
    made = None
    try:
        inp = feats.createInput(
            subject, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        if start is not None:
            inp.startExtent = start
        _set_extent(inp, mode, distance, side, taper)
        made = feats.add(inp)
        if made.bodies.count == 0:
            return None
        tbm = adsk.fusion.TemporaryBRepManager.get()
        if made.bodies.count == 1:
            return tbm.copy(made.bodies.item(0))
        # Several profiles apart make several bodies: one temporary body of them all.
        tool = tbm.copy(made.bodies.item(0))
        for i in range(1, made.bodies.count):
            tbm.booleanOperation(tool, tbm.copy(made.bodies.item(i)),
                                 adsk.fusion.BooleanTypes.UnionBooleanType)
        return tool
    except Exception:
        _log('ghost body failed:', traceback.format_exc().replace('\n', ' | '))
        return None
    finally:
        if made is not None:
            try:
                made.deleteMe()
            except Exception:
                _log('ghost cleanup failed:',
                     traceback.format_exc().replace('\n', ' | '))


def _draw_ghost(comp, body, op='cut'):
    """Paint a temporary body translucent over the model, in `op`'s colour.

    A BASIC MATERIAL effect, not a solid-colour one, and that is the whole difference.
    CustomGraphicsSolidColorEffect is documented as colouring "without any lighting
    effects", and its opacity is ignored — setOpacity on the entity does not rescue it.
    The result is a flat opaque slab that hides the very geometry the ghost exists to
    show you. Compared side by side in the viewport: solid-colour occluded the block
    completely, basic-material let its far edge read straight through.

    So the opacity lives on the material, where the API actually honours it, and the
    Phong terms give the volume enough shading to read as a solid rather than a
    silhouette.
    """
    global _ghost
    if body is None:
        return
    try:
        group = comp.customGraphicsGroups.add()
        group.id = _GHOST_ID
        entity = group.addBRepBody(body)
        # Not pickable. It starts ON the profile plane, so a click aimed at the next
        # profile hit the ghost instead — which is not extrudable, so the pick was
        # dropped and re-picking in the viewport "did nothing" on every one-sided cut.
        for g in (group, entity):
            try:
                g.isSelectable = False
            except Exception:
                pass
        diffuse, ambient = _GHOST_COLOURS.get(op, _GHOST_COLOURS['cut'])
        entity.color = adsk.fusion.CustomGraphicsBasicMaterialColorEffect.create(
            adsk.core.Color.create(*diffuse),
            adsk.core.Color.create(*ambient),
            adsk.core.Color.create(*_GHOST_SPECULAR),
            adsk.core.Color.create(*_GHOST_EMISSIVE),
            _GHOST_GLOSS,
            _GHOST_OPACITY)
        _ghost = group
    except Exception:
        _log('ghost draw failed:', traceback.format_exc().replace('\n', ' | '))


def _value(expr, fallback='0', unit=None):
    """A ValueInput from typed text, so a distance of "MembraneThickness" stays LINKED to
    that parameter rather than being frozen at whatever it reads today. In execute (the
    build that is kept — `_numbers` set) a typed number becomes a number parameter
    first (Number Params.py); previews are rolled back, so they keep the literal."""
    text = (expr or '').strip() or fallback
    if _numbers is not None and unit is not None:
        text = _numbered(text, unit)
    return adsk.core.ValueInput.createByString(text)


# {'old': old_numbers() pool, 'made': [names]} while execute builds, else None.
_numbers = None


def _length_unit(design):
    try:
        return design.unitsManager.defaultLengthUnits or 'mm'
    except Exception:
        return 'mm'


def _numbered(text, unit):
    design = adsk.fusion.Design.cast(app.activeProduct)
    try:
        new, _left, made = numbers.convert(design, text, unit, _numbers['old'])
        _numbers['made'] += made
        return new
    except Exception:
        _log('number parameter refused:', traceback.format_exc().replace('\n', ' | '))
        return text


def _orientation(design, comp, entity):
    """(inward, normal, positive_is_inward) for what is being extruded.

    Which of Fusion's two extent directions points inward. PositiveExtentDirection is
    along the profile's own normal, so this is just which side of that normal "in" is
    on — and if the rule had no answer, positive stays positive and this behaves exactly
    like Fusion's own default.
    """
    inward = _inward(design, comp, entity)
    normal, _point = _plane_of(entity)
    positive_is_inward = True
    if inward is not None and normal is not None:
        try:
            positive_is_inward = (normal.dotProduct(inward) >= 0)
        except Exception:
            positive_is_inward = True
    return inward, normal, positive_is_inward


def _auto_hide_sketches():
    """Fusion's own "Auto hide sketch on feature creation" preference."""
    try:
        prefs = adsk.fusion.FusionProductPreferences.cast(
            app.preferences.productPreferences.itemByName('Design'))
        return bool(prefs.isAutoHideSketchOnFeatureCreation)
    except Exception:
        return False


def _sketch_bulbs(entities):
    """(sketch, bulb) for every sketch a profile being extruded comes from."""
    out = []
    for e in entities:
        sk = _sketch_of(e)
        if sk is None:
            continue
        try:
            out.append((sk, sk.isLightBulbOn))
        except Exception:
            pass
    return out


def _restore_bulbs(bulbs):
    for sk, was in bulbs:
        try:
            if sk.isLightBulbOn != was:
                sk.isLightBulbOn = was
        except Exception:
            pass


def _build(design, committing=False):
    """_build_feature, with the source sketches' bulbs put back afterwards.

    Building an extrude through the API switches its profile's sketch OFF — whatever the
    auto-hide preference says, and on every preview. Measured: bulb True going in, False
    coming out, and it survives the preview's rollback. So the sketch you had just
    clicked a profile in vanished, and the next profile in it could not be picked.
    Written back INSIDE the build, where it holds; once the command commits, the
    preference decides, as it does for the native tool.
    """
    bulbs = _sketch_bulbs(_selected_entities())
    try:
        return _build_feature(design)
    finally:
        if not (committing and _auto_hide_sketches()):
            _restore_bulbs(bulbs)


def _build_feature(design):
    """Make the ExtrudeFeature the panel currently describes, or None — or, while an
    existing one is being edited, make THAT one what the panel describes.

    Called from executePreview (where Fusion throws the result away and redraws it) and
    from execute (where it is kept). Identical both times on purpose: what you saw is
    exactly what you get.

    An edit happens with the timeline rolled to just before the feature, which is where
    the API insists its profile, extent and participants are set — and, as usefully,
    where the component's bodies are the ones the feature was built AGAINST, so the
    operation rule and the participant list see the model it sees rather than its own
    result. The marker goes back to where it was before anything is judged.
    """
    global _resolved
    _clear_ghost()
    entities = _selected_entities()
    if not entities:
        return None
    # An extrusion of nothing is not a preview waiting to happen, it is an invalid
    # feature: Fusion refuses a zero extent outright. The panel opens with the distance
    # empty, so this is the ordinary state for the first second of every extrude rather
    # than an error worth reporting.
    if not (_spec.get('distance') or '').strip():
        return None

    feature = _edit_target()
    if feature is None:
        return _make(design, entities, None)

    timeline = design.timeline
    saved = timeline.markerPosition
    made = None
    try:
        feature.timelineObject.rollTo(True)
        made = _make(design, entities, feature)
    finally:
        try:
            timeline.markerPosition = saved
        except Exception:
            _log('marker restore failed:', traceback.format_exc().replace('\n', ' | '))
    if made is None:
        return None
    # An edit that Fusion accepted can still fail to COMPUTE — a distance that now cuts
    # nothing, a profile that no longer closes. That is a feature in the timeline with a
    # red flag on it, and committing it would be committing a broken model.
    try:
        if made.healthState == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
            _log('edit does not compute:', made.errorOrWarningMessage)
            return None
    except Exception:
        pass
    return made


_REACH_ATTR = ('josh.extrude', 'to')


def _to_distance(design, comp, entity, text):
    """(expression, problem) for an extrude that runs TO a datum plane: the signed
    distance, along the profile's normal, from where the profile is to the plane the
    line names. Parametric — both ends are expressions, so it follows the parameters
    behind the line (Width, BootCeiling_Inset_Top…) the way a typed distance does.

    The profile's own position is its sketch's datum line when it has one (made from
    the plane box), else the sketch plane's actual position as a number."""
    if bounds is None or comp is None:
        return '', 'The bounds module could not be loaded.'
    text = (text or '').strip()
    if not text:
        return '', 'Name a plane to extrude to.'
    normal, point = _plane_of(entity)
    axis, sign = bounds._principal_axis(normal)
    if not axis:
        return '', 'The profile is not square to an axis, so there is no "to".'
    word, _rest = bounds.planes.split_input(text) if bounds.planes else ('', '')
    d = bounds.find_datum(design, comp, word) if word else None
    if d is not None and d.get('axis') != axis:
        return '', '%s is not across this extrusion (it runs along %s).' % (word, axis)
    _base, target, problem = bounds.plane_expression(design, comp, text)
    if problem:
        return '', problem
    sketch = _sketch_of(_native(entity))
    origin = ''
    if sketch is not None:
        line = bounds.sketch_datum_text(sketch)
        if line:
            _b, expr0, p0 = bounds.plane_expression(design, comp, line)
            if not p0 and expr0:
                origin = expr0
    if not origin:
        v = {'X': point.x, 'Y': point.y, 'Z': point.z}[axis]
        origin = '%r cm' % round(v, 6)
    if sign > 0:
        return '(%s) - (%s)' % (target, origin), ''
    return '(%s) - (%s)' % (origin, target), ''


def _stamp_reach(feature):
    """Remember the "to" line on the feature, or forget it. Best-effort."""
    try:
        attrs = feature.attributes
        old = attrs.itemByName(*_REACH_ATTR)
        if _spec.get('reach') == 'to' and (_spec.get('distance') or '').strip():
            if old is not None:
                old.value = _spec['distance'].strip()
            else:
                attrs.add(_REACH_ATTR[0], _REACH_ATTR[1], _spec['distance'].strip())
        elif old is not None:
            old.deleteMe()
    except Exception:
        _log('reach stamp failed:', traceback.format_exc().replace('\n', ' | '))


# ── Following a group ────────────────────────────────────────────────────────
# An extrude made with "Bodies are: Group" (or Component) names what it follows on
# itself — Groups.py's live-set record {g: group ids, c: component id, fp, off} — and
# keeps working on exactly those bodies: after each finished command, and each filing in
# the Browser, a changed fingerprint rolls to just before the feature and sets
# participantBodies to the bodies there (Groups.Watcher / live_sync). It never moves: a
# body only there after it (a later cut split one) is left alone. None left: the feature is suppressed until
# there are some again. A group that is gone leaves it as it is. The Browser add-in
# reads the same attribute to warn before a group is exploded (_group_uses).

_GROUPS_ATTR = ('josh.extrude', 'groups')


def _stamp_groups(design, feature, comp, in_groups):
    """Name the groups — or the component `comp` — on the feature, or forget them.
    Best-effort."""
    try:
        attrs = feature.attributes
        old = attrs.itemByName(*_GROUPS_ATTR)
        gids = list(_spec.get('groups') or []) if in_groups else []
        rec = ({'g': [], 'c': comp.id} if comp is not None
               else {'g': gids, 'c': ''} if gids else None)
        if rec is not None:
            rec['fp'] = groups.live_fingerprint(design, rec)
            rec['off'] = False
            attrs.add(_GROUPS_ATTR[0], _GROUPS_ATTR[1], json.dumps(rec))
        elif old is not None:
            old.deleteMe()
    except Exception:
        _log('groups stamp failed:', traceback.format_exc().replace('\n', ' | '))


def _sync_groups(design):
    """Every extrude following groups whose fingerprint moved, brought into step. Main
    thread, idle Fusion, marker at the end (Groups.Watcher)."""
    try:
        attrs = list(design.findAttributes(*_GROUPS_ATTR))
    except Exception:
        return
    FO = adsk.fusion.FeatureOperations
    for a in attrs:
        try:
            f = adsk.fusion.ExtrudeFeature.cast(a.parent)
            if f is None or not f.isValid:
                continue
            rec = json.loads(a.value)
        except Exception:
            continue
        groups.unpin(f, tickguard.one_step('ExtrudeUnpin'))
        if groups.in_step(design, f, rec):             # unchanged, or changed only after it
            continue
        if groups.live_gone(design, rec):
            _log('groups: what', f.name, 'follows is gone — left as it is')
            continue
        to = f.timelineObject
        if to.isSuppressed and not rec.get('off'):
            continue                                   # suppressed by hand: leave it
        if f.operation not in (FO.JoinFeatureOperation, FO.CutFeatureOperation,
                               FO.IntersectFeatureOperation):
            continue

        def apply(feat, parts):
            feat.participantBodies = parts

        try:
            with tickguard.hold('extrude/groups'), tickguard.one_step('ExtrudeGroups'):
                n = groups.live_sync(design, f, rec, apply, _log)
                f.attributes.add(_GROUPS_ATTR[0], _GROUPS_ATTR[1], json.dumps(rec))
            _log('groups:', f.name, '->', n, 'bodies' if n else 'bodies (suppressed)')
        except Exception:
            _log('groups sync failed:', traceback.format_exc().replace('\n', ' | '))


def _groups_busy():
    return bool(_running) or tickguard.busy() or tickguard._in_sketch()


_FLEX_GROUP = 'FusionFlex'      # Section.py's FLEX: attribute group on user parameters


def _read_flexes(design):
    """{param name: {flexible, min, max}} for every user parameter with a range."""
    out = {}
    try:
        ups = design.userParameters
        for i in range(ups.count):
            p = ups.item(i)
            try:
                a = p.attributes.itemByName(_FLEX_GROUP, 'range')
                if a is not None and a.value:
                    d = json.loads(a.value)
                    d.setdefault('flexible', True)
                    out[p.name] = d
            except Exception:
                pass
    except Exception:
        pass
    return out


def _write_flex(design):
    """The panel's Flexible row, onto the parameter the distance is. Same record
    Section.write_flex keeps: absent = flexible with no limits."""
    f = _spec.get('flex')
    if not f or _spec.get('reach') == 'to':
        return
    name = str(f.get('name') or '')
    if name != (_spec.get('distance') or '').strip():
        return
    try:
        p = design.userParameters.itemByName(name)
        if p is None:
            return
        lo, hi = str(f.get('min') or ''), str(f.get('max') or '')
        flexible = bool(f.get('flexible', True))
        if flexible and not lo and not hi:
            a = p.attributes.itemByName(_FLEX_GROUP, 'range')
            if a is not None:
                a.deleteMe()
        else:
            p.attributes.add(_FLEX_GROUP, 'range',
                             json.dumps({'flexible': flexible, 'min': lo, 'max': hi}))
    except Exception:
        _log('flex write failed:', traceback.format_exc().replace('\n', ' | '))


def _make(design, entities, editing):
    """The body of _build: work out the feature, then create it — or, with `editing`,
    apply it to that existing feature. The timeline is already where it needs to be."""
    global _resolved
    entity = entities[0]
    comp = _component_of(entity)
    if comp is None:
        return None

    inward, normal, positive_is_inward = _orientation(design, comp, entity)

    directions = adsk.fusion.ExtentDirections
    side = (directions.PositiveExtentDirection if positive_is_inward
            else directions.NegativeExtentDirection)

    travel = _travel(normal, positive_is_inward)

    feats = comp.features.extrudeFeatures
    # More than one profile goes in as a collection, which is how the native tool takes
    # them too. The component, the direction and the operation all come from the FIRST —
    # they are properties of where the profiles are, and a selection spanning two planes
    # is not something either tool can extrude in one go anyway.
    if len(entities) > 1:
        subject = adsk.core.ObjectCollection.create()
        for e in entities:
            subject.add(e)
    else:
        subject = entity

    chosen = _spec.get('operation', 'auto')
    if chosen == 'auto':
        operation = _auto_operation(comp, entity, travel)
    else:
        operation = _OPERATION_WORD.get(chosen,
                                        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

    # Counted BEFORE the add, or a new body would be counted as one that was already
    # there — which is the opposite of what the number means.
    own = _own_bodies(comp)
    keys = _body_keys(own)

    # A different component under a re-pick: what was chosen there means nothing here.
    if (editing is None and _resolved.get('component')
            and _resolved.get('component') != comp.name):
        _spec['exclude'] = []
        _spec['bodiesAuto'] = True
    if editing is not None:
        _seed_participants(editing, own)

    start = None
    if _spec.get('start') == 'offset':
        try:
            start = adsk.fusion.OffsetStartDefinition.create(
                _value(_spec.get('startOffset'), '0', _length_unit(design)))
        except Exception:
            _log('start offset refused:', traceback.format_exc().replace('\n', ' | '))

    taper = _value(_spec.get('taper'), '0 deg', 'deg')
    distance = _value(_spec.get('distance'), '0', _length_unit(design))
    mode = _spec.get('direction', 'one')
    if _spec.get('reach') == 'to':
        # To a plane: always one side, along the profile's normal; the sign of the
        # distance carries the direction.
        text, why = _to_distance(design, comp, entity, _spec.get('distance'))
        if why:
            _log('to-plane refused:', why)
            _send('/extrude/error', {'error': why})
            return None
        distance = _value(text, '0', _length_unit(design))
        mode = 'one'
        side = directions.PositiveExtentDirection

    # This component's own bodies, and nothing else — see the module docstring. Only
    # meaningful for the operations that consume existing material; setting it for a new
    # body is at best noise.
    FO = adsk.fusion.FeatureOperations
    targeted = operation in (FO.JoinFeatureOperation, FO.CutFeatureOperation,
                             FO.IntersectFeatureOperation)
    ghosted = (operation in (FO.CutFeatureOperation, FO.IntersectFeatureOperation)
               and mode == 'one')
    in_comp = targeted and _spec.get('bodiesScope') == 'component'
    auto = (targeted and editing is None and _spec.get('bodiesAuto', True) and bool(own)
            and not in_comp)
    # BEFORE the real feature, because a cut destroys the very volume being drawn.
    tool = (_tool_body(comp, subject, mode, side, taper, distance, start)
            if ghosted or auto else None)
    if auto:
        _spec['exclude'] = _auto_exclude(own, keys, tool, operation)
        # Reaches nothing (a cut you forced where the rule said new body): hand Fusion
        # every visible body, as before, and let IT say the cut misses — an automatic
        # list must never be the reason nothing builds.
        if len(_spec['exclude']) == len(own):
            _spec['exclude'] = _auto_exclude(own, keys, None, operation)
    # "Bodies are: Group" — the chosen groups' bodies, whatever was clicked; while still
    # automatic, the groups of what the extrusion runs into.
    in_groups = targeted and _spec.get('bodiesScope') == 'group'
    if in_groups:
        if auto:
            out = set(_spec['exclude'])
            _spec['groups'] = _groups_of(design, comp,
                                         [b for b, k in zip(own, keys) if k not in out])
        want = _group_keys(design)
        mine = [b for k, b in groups.keyed_bodies(comp) if k in want]
        _spec['exclude'] = [k for b, k in zip(own, keys) if not any(b == m for m in mine)]
    # "Bodies are: Component" — all of them.
    if in_comp:
        _spec['exclude'] = []
    participants = []
    if targeted:
        left_out = set(_spec.get('exclude') or [])
        participants = [b for b, k in zip(own, keys) if k not in left_out]

    _resolved = {
        'operation': _WORD_OPERATION.get(operation, 'newbody'),
        'operationAuto': chosen == 'auto',
        'inward': inward is not None,
        'flipped': not positive_is_inward,
        'component': comp.name,
        'componentId': comp.id,
        'bodies': len(own),
        'bodyNames': keys,
        # Nothing to work on. An empty participantBodies means ALL of them to Fusion,
        # so this cannot be handed over as it stands — it builds nothing instead, and
        # the commit says why.
        'noTargets': targeted and bool(own) and not participants,
    }
    if _resolved['noTargets']:
        return None

    # For one side only, which is the shape the ghost is worth having for.
    if ghosted:
        _draw_ghost(comp, tool, _WORD_OPERATION.get(operation, 'cut'))

    if editing is None:
        inp = feats.createInput(subject, operation)
        if participants:
            try:
                inp.participantBodies = participants
            except Exception:
                _log('participantBodies refused:',
                     traceback.format_exc().replace('\n', ' | '))
        try:
            inp.isSolid = bool(_spec.get('isSolid', True))
        except Exception:
            pass
        if start is not None:
            inp.startExtent = start
        _set_extent(inp, mode, distance, side, taper)
        feature = feats.add(inp)
        _stamp_reach(feature)
        _stamp_groups(design, feature, comp if in_comp else None, in_groups)
        if operation == FO.NewBodyFeatureOperation and defapp is not None:
            # Preview and execute alike (what you saw is what you get): the preview's
            # write rolls back with it, execute's lands in the command's own transaction
            # — so the Browser's after-the-fact paint finds nothing left to do.
            try:
                defapp.paint_new(design, list(feature.bodies))
            except Exception:
                _log('default appearance failed:',
                     traceback.format_exc().replace('\n', ' | '))
    else:
        feature = editing
        _apply_edit(feature, entities, subject, operation, participants, start,
                    mode, distance, side, taper)
        _stamp_reach(feature)
        _stamp_groups(design, feature, comp if in_comp else None, in_groups)
    return feature


def _set_extent(target, mode, distance, side, taper, full_length=False):
    """The extent, on an ExtrudeFeatureInput or an existing ExtrudeFeature — both have
    the same three setters with the same arguments."""
    if mode == 'symmetric':
        # isFullLength False: the number is the distance to EACH side, which is what a
        # distance means everywhere else in this panel.
        target.setSymmetricExtent(distance, full_length, taper)
    elif mode == 'two':
        target.setTwoSidesExtent(
            adsk.fusion.DistanceExtentDefinition.create(distance),
            adsk.fusion.DistanceExtentDefinition.create(
                _value(_spec.get('distance2'), '0')),
            taper, taper)
    else:
        target.setOneSideExtent(
            adsk.fusion.DistanceExtentDefinition.create(distance), side, taper)


# ── Editing an existing extrude ──────────────────────────────────────────────
# Edit Feature on an extrude — the timeline's right-click, a double-click on it — starts
# FusionExtrudeEditCommand, which is Fusion's own dialog. It is refused in
# commandStarting and THIS command starts instead, seeded from the feature, with every
# preview and the final OK applied to that feature rather than making a new one.
#
# EDITED, NOT REPLACED. Deleting the feature and building a fresh one would be simpler
# and would break everything downstream that references it: its faces, its edges, its
# parameters by name. So values are written back into the feature's OWN parameters
# wherever the shape of the extent is unchanged, and only a change of shape (one side to
# two, a start that becomes an offset) resets the extent through the setters.
#
# Only what the panel can say is taken over. An extent that runs to an object, "all",
# a start from an entity, a thin extrude — those open Fusion's dialog exactly as before,
# because a panel that silently dropped them would change the feature on OK.
#
# Also reachable without the timeline: select an extrude (timeline or browser) and start
# this command — the same edit.

EDIT_CMD_IDS = ('FusionExtrudeEditCommand', 'FusionEditFeatureCommand')
_EDIT_EVENT_ID = 'SmartExtrudeEditLaunch'

# The feature being edited, or None for an ordinary extrude. Kept as a token too:
# previews roll the model back and forth, and a Python wrapper that has gone stale is
# re-found by token rather than trusted.
_editing = None
_edit = {}              # token, seed spec, original shape — see _edit_plan

# Handed from commandStarting to commandCreated. Set, then the launch event fires, then
# the command's own commandCreated takes it.
_pending_edit = None

_starting = None
_edit_evt = None
_edit_launch = None


def _edit_target():
    """The feature being edited, valid, or None."""
    global _editing
    if _editing is None:
        return None
    try:
        if _editing.isValid:
            return _editing
    except Exception:
        pass
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        found = design.findEntityByToken(_edit.get('token', '')) if design else []
        for ent in found or []:
            feat = adsk.fusion.ExtrudeFeature.cast(ent)
            if feat is not None:
                _editing = feat
                return feat
    except Exception:
        _log('edit target lost:', traceback.format_exc().replace('\n', ' | '))
    return None


def _extrude_feature_in(entity):
    """An ExtrudeFeature from a selection entry: the feature itself, or the timeline
    item standing for it."""
    try:
        feat = adsk.fusion.ExtrudeFeature.cast(entity)
        if feat is not None:
            return feat
        tlo = adsk.fusion.TimelineObject.cast(entity)
        if tlo is not None:
            return adsk.fusion.ExtrudeFeature.cast(tlo.entity)
    except Exception:
        pass
    return None


def _selected_extrude():
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            feat = _extrude_feature_in(sel.item(i).entity)
            if feat is not None:
                return feat
    except Exception:
        pass
    return None


def _param_text(param):
    try:
        return param.expression if param is not None else ''
    except Exception:
        return ''


def _profiles_of(feature):
    """What the feature was extruded FROM, as the list _picked holds."""
    prof = feature.profile
    coll = adsk.core.ObjectCollection.cast(prof)
    if coll is not None:
        return [coll.item(i) for i in range(coll.count)]
    return [prof] if prof is not None else []


def _edit_plan(feature):
    """(spec, meta, profiles) for editing `feature` with this panel, or None when it has
    something the panel cannot express — the caller then leaves Fusion's dialog alone.

    Read, never written: this runs inside commandStarting, before anything is refused.
    """
    FET = adsk.fusion.FeatureExtentTypes
    try:
        if not feature.isValid or not feature.isSolid or feature.isThinExtrude:
            return None
        spec = dict(_DEFAULT_SPEC)
        meta = {'fullLength': False}
        kind = feature.extentType
        if kind == FET.OneSideFeatureExtentType:
            one = adsk.fusion.DistanceExtentDefinition.cast(feature.extentOne)
            if one is None:
                return None
            spec['direction'] = 'one'
            spec['distance'] = _param_text(one.distance)
            meta['value'] = one.distance.value
            try:
                stamp = feature.attributes.itemByName(*_REACH_ATTR)
                if stamp is not None and stamp.value:
                    spec['reach'] = 'to'          # the field is the line, not the length
                    spec['distance'] = stamp.value
            except Exception:
                pass
        elif kind == FET.TwoSidesFeatureExtentType:
            one = adsk.fusion.DistanceExtentDefinition.cast(feature.extentOne)
            two = adsk.fusion.DistanceExtentDefinition.cast(feature.extentTwo)
            if one is None or two is None:
                return None
            spec['direction'] = 'two'
            spec['distance'] = _param_text(one.distance)
            spec['distance2'] = _param_text(two.distance)
        elif kind == FET.SymmetricFeatureExtentType:
            sym = feature.symmetricExtent
            if sym is None:
                return None
            spec['direction'] = 'symmetric'
            spec['distance'] = _param_text(sym.distance)
            meta['fullLength'] = bool(sym.isFullLength)
        else:
            return None

        start = feature.startExtent
        if adsk.fusion.OffsetStartDefinition.cast(start) is not None:
            spec['start'] = 'offset'
            spec['startOffset'] = _param_text(
                adsk.fusion.OffsetStartDefinition.cast(start).offset)
        elif adsk.fusion.ProfilePlaneStartDefinition.cast(start) is None:
            return None

        # A taper of nothing reads as an empty field, which is how the panel opens.
        try:
            taper = feature.taperAngleOne
            if taper is not None and abs(taper.value) > 1e-9:
                spec['taper'] = taper.expression
        except Exception:
            pass

        # The operation you gave it, not 'auto': an edit keeps what the feature does
        # unless you change it.
        spec['operation'] = _WORD_OPERATION.get(feature.operation, 'newbody')
        spec['isSolid'] = True
        # Made following Browser groups: it still does (see "Following a group").
        try:
            stamp = feature.attributes.itemByName(*_GROUPS_ATTR)
            rec = json.loads(stamp.value) if stamp is not None else {}
            gids = rec.get('g')
            if rec.get('c'):
                spec['bodiesScope'] = 'component'
                spec['bodiesAuto'] = False
            elif gids:
                spec['bodiesScope'] = 'group'
                spec['groups'] = list(gids)
                spec['bodiesAuto'] = False
        except Exception:
            pass

        profiles = [p for p in _profiles_of(feature) if _extrudable(p)]
        if not profiles:
            return None
        meta['token'] = feature.entityToken
        meta['profiles'] = _identity(profiles)
        # Until the first preview has measured which way the feature really goes — see
        # _settle_direction.
        meta['settled'] = spec['direction'] != 'one'
        meta['sideMatches'] = True
        return spec, meta, profiles
    except Exception:
        _log('edit plan failed:', traceback.format_exc().replace('\n', ' | '))
        return None


def _centre(faces, most=8):
    """The centre of a set of faces' boxes, or None for an empty set.

    From at most `most` of them, spread along the set. A cut through a rib grid has
    500-odd side faces and a boundingBox read each took 4 seconds; _travel_sign only
    wants a sign, and every side face lies between the start and end planes, so a
    handful gives the same answer."""
    pts = []
    try:
        n = faces.count
        step = max(1, n // most)
        for i in range(0, n, step):
            box = faces.item(i).boundingBox
            pts.append(((box.minPoint.x + box.maxPoint.x) / 2,
                        (box.minPoint.y + box.maxPoint.y) / 2,
                        (box.minPoint.z + box.maxPoint.z) / 2))
    except Exception:
        return None
    if not pts:
        return None
    n = float(len(pts))
    return tuple(sum(p[k] for p in pts) / n for k in range(3))


def _travel_sign(feature, normal):
    """+1 when the feature really goes along the profile normal, -1 against it, None
    when that cannot be told. Measured from its faces, with the timeline just after it,
    so later features have not yet eaten them.

    Measured, because the API has no getter for a one-sided extrude's direction: it is
    set as an argument and then not stored anywhere readable. Pairs are compared, never
    a face against the sketch plane, so a start offset cannot fool it.
    """
    end = _centre(feature.endFaces)
    sides = _centre(feature.sideFaces)
    start = _centre(feature.startFaces)
    for far, near in ((end, sides), (sides, start), (end, start)):
        if far is None or near is None:
            continue
        d = sum((far[k] - near[k]) * v for k, v in
                enumerate((normal.x, normal.y, normal.z)))
        if abs(d) > 1e-6:
            return 1 if d > 0 else -1
    return None


def _wraps(text):
    """Is `text` one "(...)" pair around the whole of it — not "(a) + (b)"?"""
    if not (text.startswith('(') and text.endswith(')')):
        return False
    depth = 0
    for i, c in enumerate(text):
        depth += c == '('
        depth -= c == ')'
        if depth == 0 and i < len(text) - 1:
            return False
    return depth == 0


def _negate(expr):
    """The expression for minus `expr`, peeling a sign rather than stacking one.

    Every sign and every whole-wrapping bracket is taken off first, so negating
    "-(-(Width))" is "Width", not a fourth layer: each edit of a flipped extrude
    negated the stored text again, and the box showed every layer as a bubble.
    """
    text = (expr or '').strip()
    if not text:
        return text
    negative = True
    while True:
        if _wraps(text):
            text = text[1:-1].strip()
        elif text.startswith('-') and not text.startswith('--'):
            rest = text[1:].strip()
            if not (_wraps(rest) or not any(c in rest for c in '+-*/()')):
                break
            negative = not negative
            text = rest
        else:
            break
    if not negative:
        return text
    simple = not any(c in text for c in '+-*/()')
    return ('-' + text) if simple else '-(' + text + ')'


def _settle_direction(design, may_roll):
    """Put a one-sided edit into this panel's terms: positive is IN.

    The feature's own distance is signed against whichever side Fusion was told, which
    may be either — a native extrude knows nothing of bounds. So its real direction is
    measured (see _travel_sign) and, where that disagrees with the inward rule, the
    distance is shown negated: what you read is what this tool would build.

    Measured where the model stands first, which changes nothing and is nearly always
    enough. Only when later features have eaten every face it would read is the
    timeline rolled to just after it — a recompute, so only with `may_roll`, which is
    inside a preview: that is the one place Fusion takes a model change back afterwards.
    Without it, and with preview on, the question is left for the preview to settle;
    with preview off it takes the reading that needs no measurement.
    """
    if _edit.get('settled'):
        return
    feature = _edit_target()
    if feature is None or not _picked:
        _edit['settled'] = True
        return
    entity = _picked[0]
    comp = _component_of(entity)
    _inward_v, normal, positive_is_inward = _orientation(design, comp, entity)
    if normal is None:
        _edit['settled'] = True
        return
    actual = None
    try:
        actual = _travel_sign(feature, normal)
    except Exception:
        _log('measure failed:', traceback.format_exc().replace('\n', ' | '))
    if actual is None and may_roll:
        timeline = design.timeline
        saved = timeline.markerPosition
        try:
            feature.timelineObject.rollTo(False)
            actual = _travel_sign(feature, normal)
        except Exception:
            _log('measure failed:', traceback.format_exc().replace('\n', ' | '))
        finally:
            try:
                timeline.markerPosition = saved
            except Exception:
                pass
    elif actual is None and _spec.get('preview', True):
        return
    _edit['settled'] = True
    value = _edit.get('value') or 0
    sign = 1 if value >= 0 else -1
    # Which side Fusion holds the feature on, relative to the normal. With nothing
    # measurable, the reading that needs no measurement: the sign is in the value.
    stored = (actual * sign) if actual is not None else 1
    ours = 1 if positive_is_inward else -1
    _edit['sideMatches'] = (stored == ours)
    if stored != ours:
        _spec['distance'] = _negate(_spec.get('distance'))
        _edit['seed']['distance'] = _spec['distance']
    _log('edit direction: measured', actual, 'value sign', sign,
         'stored', stored, 'ours', ours)


def _apply_edit(feature, entities, subject, operation, participants, start,
                mode, distance, side, taper):
    """Make `feature` what the panel says. Rolled to just before it by _build.

    Each thing is written only if the panel CHANGED it, and into the feature's own
    parameter where it can be: an edit you did not make is not an edit.
    """
    seed = _edit.get('seed') or {}

    def changed(key):
        return (str(_spec.get(key) or '')).strip() != (str(seed.get(key) or '')).strip()

    if _identity(entities) != _edit.get('profiles'):
        feature.profile = subject

    exclude_changed = (sorted(_spec.get('exclude') or []) !=
                       sorted(seed.get('exclude') or []))
    op_changed = feature.operation != operation
    if op_changed:
        feature.operation = operation
    if op_changed or exclude_changed:
        if participants:
            try:
                feature.participantBodies = participants
            except Exception:
                _log('participantBodies refused:',
                     traceback.format_exc().replace('\n', ' | '))

    if _spec.get('start') != seed.get('start'):
        feature.startExtent = (start if start is not None else
                               adsk.fusion.ProfilePlaneStartDefinition.create())
    elif _spec.get('start') == 'offset' and changed('startOffset'):
        adsk.fusion.OffsetStartDefinition.cast(feature.startExtent).offset.expression = (
            (_spec.get('startOffset') or '').strip() or '0')

    full = _edit.get('fullLength', False) if seed.get('direction') == 'symmetric' else False
    if _spec.get('reach') == 'to' or seed.get('reach') == 'to':
        # A "to" distance is computed (a difference of two plane expressions), never a
        # field to write back into: set the extent whole, same as a change of shape.
        _set_extent(feature, mode, distance, side, taper, full)
        return
    if mode != seed.get('direction'):
        _set_extent(feature, mode, distance, side, taper, full)
        return
    if not any(changed(k) for k in ('distance', 'distance2', 'taper')):
        return
    if mode == 'one' and not _edit.get('sideMatches', True):
        # Shown negated (see _settle_direction), so it cannot be written back as typed.
        _set_extent(feature, mode, distance, side, taper, full)
        return
    try:
        if changed('distance'):
            ext = (feature.symmetricExtent if mode == 'symmetric' else
                   adsk.fusion.DistanceExtentDefinition.cast(feature.extentOne))
            ext.distance.expression = _spec['distance'].strip()
        if mode == 'two' and changed('distance2'):
            adsk.fusion.DistanceExtentDefinition.cast(
                feature.extentTwo).distance.expression = (
                    (_spec.get('distance2') or '').strip() or '0')
        if changed('taper'):
            text = (_spec.get('taper') or '').strip() or '0 deg'
            feature.taperAngleOne.expression = text
            if mode == 'two':
                feature.taperAngleTwo.expression = text
    except Exception:
        _log('in-place edit refused, resetting extent:',
             traceback.format_exc().replace('\n', ' | '))
        _set_extent(feature, mode, distance, side, taper, full)


def _begin_edit():
    """Seed the command just created from the feature it is editing, if it is editing
    one. Called from commandCreated, after the ordinary reset."""
    global _pending_edit, _editing, _edit, _spec, _picked, _picked_id
    pending, _pending_edit = _pending_edit, None
    _editing = None
    _edit = {}
    if pending is None:
        feature = _selected_extrude()
        plan = _edit_plan(feature) if feature is not None else None
        if plan is None:
            return
        pending = (feature,) + plan
    feature, spec, meta, profiles = pending
    _editing = feature
    _edit = dict(meta)
    _spec = dict(spec)
    _edit['seed'] = dict(spec)
    _picked = list(profiles)
    _picked_id = _identity(_picked)
    _spec['preview'] = not _too_heavy(feature)
    _log('editing', feature.name, spec, '' if _spec['preview'] else '(preview off)')


def _too_heavy(feature):
    """Should an edit of `feature` open with preview off? Read-only and instant: its
    face count, and what its last build here was measured at."""
    try:
        if _build_secs.get(feature.entityToken, 0) > _HEAVY_SECS:
            return True
        return feature.faces.count > _HEAVY_FACES
    except Exception:
        return False


def _note_build(seconds):
    """Remember how long an edit's build took, for _too_heavy next time."""
    token = _edit.get('token')
    if token:
        _build_secs[token] = seconds


class _EditStarting(adsk.core.ApplicationCommandEventHandler):
    """Refuse Fusion's extrude edit and start ours on the same feature."""
    def notify(self, args):
        global _pending_edit
        try:
            if args.commandId not in EDIT_CMD_IDS or _running:
                return
            feature = _selected_extrude()
            if feature is None:
                if args.commandId == EDIT_CMD_IDS[0]:
                    _log('native extrude edit with no extrude selected — left alone')
                return
            plan = _edit_plan(feature)
            if plan is None:
                _log('extrude edit left to Fusion: nothing this panel can express')
                return
            args.isCanceled = True
            _pending_edit = (feature,) + plan
            # Out of this handler before our command is started: execute() on another
            # command from inside commandStarting is not something to lean on.
            app.fireCustomEvent(_EDIT_EVENT_ID, '')
        except Exception:
            _log('edit starting failed:', traceback.format_exc().replace('\n', ' | '))


class _EditLaunch(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            cmd_def = ui.commandDefinitions.itemById(CMD_ID)
            if cmd_def is not None and _pending_edit is not None:
                cmd_def.execute()
        except Exception:
            _log('edit launch failed:', traceback.format_exc().replace('\n', ' | '))


# ── Getting the native palette off the screen ────────────────────────────────
# A Fusion command with inputs shows Fusion's dialog, and this one has a selection input
# it cannot do without: picking in the viewport is driven by the command's selection
# machinery, and that machinery only runs for a real SelectionCommandInput. The input has
# to EXIST; it does not have to be looked at.
#
# So the palette is hidden at the Qt level, which is the same lever Maximize Viewport
# uses on the title bar and the tabs. Two steps, because either alone leaves something
# behind: setDialogSize shrinks it but Fusion keeps a frame, and hiding the widget without
# shrinking leaves a hole in the layout that flashes on the next relayout.
#
# GUI THREAD ONLY. Sampling the Qt widget tree from a background thread races widget
# deletion and segfaults Fusion (see fusion_bg_thread_qt_crash) — everything here is
# called from a command handler or the custom event, both of which are the main thread.

_qt = None


class _Qt:
    """The little of QtWidgets this needs. A local copy rather than a shared module: the
    only other user is Maximize Viewport, and reaching into a running add-in to borrow
    six symbols is a worse dependency than thirty lines."""

    def __init__(self):
        libc = ctypes.CDLL(None)
        libc._dyld_image_count.restype = ctypes.c_uint32
        libc._dyld_get_image_name.restype = ctypes.c_char_p
        libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]

        def image(substr):
            b = substr.encode()
            for i in range(libc._dyld_image_count()):
                n = libc._dyld_get_image_name(i)
                if n and b in n:
                    return n.decode()
            return None

        qtw = ctypes.CDLL(image('QtWidgets.framework/QtWidgets'))
        qtc = ctypes.CDLL(image('QtCore.framework/QtCore'))

        class QWidgetList(ctypes.Structure):
            _fields_ = [('d', ctypes.c_void_p), ('ptr', ctypes.c_void_p),
                        ('size', ctypes.c_longlong)]

        class QString(ctypes.Structure):
            _fields_ = [('d', ctypes.c_void_p), ('ptr', ctypes.c_void_p),
                        ('size', ctypes.c_longlong)]

        self._all = qtw['_ZN12QApplication10allWidgetsEv']
        self._all.restype = QWidgetList
        self._all.argtypes = []
        self._name = qtc['_ZNK7QObject10objectNameEv']
        self._name.restype = QString
        self._name.argtypes = [ctypes.c_void_p]
        self._meta = qtc['_ZNK11QMetaObject9classNameEv']
        self._meta.restype = ctypes.c_char_p
        self._meta.argtypes = [ctypes.c_void_p]
        self._setVisible = qtw['_ZN7QWidget10setVisibleEb']
        self._setVisible.restype = None
        self._setVisible.argtypes = [ctypes.c_void_p, ctypes.c_bool]

    def widgets(self):
        lst = self._all()
        if not lst.ptr or not (0 < lst.size < 200000):
            return []
        arr = (ctypes.c_void_p * lst.size).from_address(lst.ptr)
        return [arr[i] for i in range(lst.size) if arr[i]]

    def name(self, w):
        qs = self._name(w)
        if qs.ptr and 0 < qs.size < 4096:
            return bytes((ctypes.c_char * (qs.size * 2)).from_address(
                qs.ptr)).decode('utf-16-le', 'replace')
        return ''

    def cls(self, w):
        try:
            vt = ctypes.c_void_p.from_address(w).value
            fn = ctypes.c_void_p.from_address(vt).value
            mo = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(fn)(w)
            return (self._meta(mo) or b'').decode('utf-8', 'replace')
        except Exception:
            return ''

    def hide(self, w):
        self._setVisible(w, False)


def _qt_bind():
    global _qt
    if _qt is None:
        try:
            _qt = _Qt()
        except Exception:
            _log('no Qt bind:', traceback.format_exc().replace('\n', ' | '))
            _qt = False
    return _qt or None


def _dump_widgets(tag):
    """Every widget's class and object name, to the log. Only ever called by hand while
    working out WHICH widget the command palette is — left in because that question comes
    back every time Fusion's UI is rebuilt."""
    q = _qt_bind()
    if q is None:
        return
    try:
        seen = []
        for w in q.widgets():
            seen.append('%s | %s' % (q.cls(w), q.name(w)))
        _log('WIDGETS', tag, len(seen))
        for line in sorted(set(seen)):
            _log('   ', line)
    except Exception:
        _log('dump failed:', traceback.format_exc().replace('\n', ' | '))


# Fusion names the palette after the command inside it —
#     QTPalette  QTToolFrame:QTCommandDialogContentPanelSmartExtrudeCmd
# — which is the whole reason this can be done by name at all. Matching on the CLASS
# would be a disaster: every palette in Fusion is a QTPalette, the Browser included.
# Read off a live dump, not guessed; _dump_widgets is how to find it again if a Fusion
# update renames it.
_PALETTE_MARK = 'QTCommandDialogContentPanel' + CMD_ID


def _hide_palette():
    """Take Fusion's own dialog off the screen. Best effort: with it still showing this
    works exactly as well, it is just ugly — never a reason to fail the command.

    All four of the command's widgets are hidden, not only the palette: the title bar and
    the resize handle are siblings rather than children, so hiding the frame alone leaves
    a stub of chrome behind.
    """
    q = _qt_bind()
    if q is None:
        return
    hidden = 0
    try:
        for w in q.widgets():
            if _PALETTE_MARK in q.name(w):
                q.hide(w)
                hidden += 1
    except Exception:
        _log('hide failed:', traceback.format_exc().replace('\n', ' | '))
    if not hidden:
        _log('palette not found — looked for', _PALETTE_MARK)


# ── The panel ────────────────────────────────────────────────────────────────

def _params_snapshot(design):
    """The parameter list the panel's distance and taper fields complete against."""
    out = []
    try:
        for p in design.allParameters:
            try:
                out.append({'name': p.name, 'expression': p.expression,
                            'unit': p.unit, 'value': p.value,
                            'comment': p.comment or ''})
            except Exception:
                continue
    except Exception:
        pass
    return out


def _naming_situation(design, entity, comp):
    """What the panel's parameter naming needs to know about THIS extrude — the same
    answer a dimension session carries (axis/datum/datumFaces), worked out from the
    profile's plane: the axis its normal is square to, and what the plane sits on there
    (the origin, or one of the component's bound faces). {} when untellable — the panel
    then resets to no opinion rather than keeping the last dimension's."""
    try:
        if bounds is None or entity is None or comp is None:
            return {}
        normal, point = _plane_of(entity)
        axis, _sign = bounds._principal_axis(normal)
        if not axis:
            return {}
        v = {'X': point.x, 'Y': point.y, 'Z': point.z}[axis]
        face = bounds.face_at(design, comp, axis, v) or ''
        zero = abs(v) < 1e-4
        return {'axis': axis, 'end': '', 'axes': [axis], 'ends': {},
                'datum': 'zero' if zero else face,
                'datumFaces': [face] if (zero and face) else [],
                'symmetric': bool(bounds.is_symmetric(design, comp, axis))}
    except Exception:
        return {}


def _open_panel(design):
    """Arm the extrude panel with the situation already worked out — see
    server.open_extrude_session for why none of this is queued."""
    entities = _selected_entities()
    entity = entities[0] if entities else None
    comp = _component_of(entity) if entity is not None else None
    # Still on the rule: what it lands on for THIS pick, worked out now rather than left
    # to the first preview — that waits for a distance, and the panel opens with one of
    # its operations already selected (there is no Auto button to show instead).
    resolved = _resolved.get('operation', '')
    if _spec.get('operation', 'auto') == 'auto' and entity is not None:
        resolved = _auto_word(design) or resolved
    spec = dict(_spec)
    spec.update({
        'unit': design.unitsManager.defaultLengthUnits,
        'hasSelection': bool(entities),
        'selectionCount': len(entities),
        'component': comp.name if comp is not None else '',
        'inward': _inward(design, comp, entity) is not None if entity is not None else False,
        'operationResolved': resolved,
        'operationAuto': True,
        'bodyNames': _resolved.get('bodyNames', []),
        'noTargets': _resolved.get('noTargets', False),
        'groupChips': _panel_group_chips(design),
        'situation': _naming_situation(design, entity, comp),
        'flexes': _read_flexes(design),
        # The chords Active While Typing owns right now, so the panel can catch one
        # itself — Fusion's own keymap never sees a key while this window has focus,
        # because the real Extrude dialog it stands in for is hidden. See
        # fusion_value/index.js's keydown hook and hotkeys.py::typing_keys/press.
        'keys': _hotkeys().typing_keys(),
        # The chords of those that step a row (Cycle Operation / Direction): the panel
        # steps the row itself on one, rather than wait for the round trip.
        'cycleKeys': _hotkeys().cycle_keys(),
        'rev': _rev[0],
    })
    _send('/extrude/open', {'spec': spec, 'params': _params_snapshot(design)})
    _opened[0] = True


def _push_state():
    """Tell the panel where things stand. Not a re-arm — see server.update_extrude.

    Two kinds of thing go back. What the last preview DID (the operation the rule landed
    on, which way it went) has always gone this way. What the panel and this side both
    own — the operation and direction words, the number of things picked — goes with it
    now, because a key can change any of them from out here: cycle the operation with the
    panel open and the row has to move, and a panel showing 'auto' over an extrude that
    is cutting is worse than no panel at all.

    Only ever the CHOICES and the counts, never the expression fields: those may be
    half-typed at this instant, and the panel's own text is the truth for them.
    """
    entities = _selected_entities()
    spec = {
        'operation': _spec.get('operation', 'auto'),
        'direction': _spec.get('direction', 'one'),
        'preview': _spec.get('preview', True),
        # Ours too: an edit seeds it from the feature on its first build, and a re-pick
        # into another component empties it.
        'exclude': list(_spec.get('exclude') or []),
        'bodiesScope': _spec.get('bodiesScope', 'bodies'),
        'groups': list(_spec.get('groups') or []),
        'groupChips': _panel_group_chips(adsk.fusion.Design.cast(app.activeProduct)),
        'hasSelection': bool(entities),
        'selectionCount': len(entities),
        # Resent every time: a binding changed while the panel was open (Active While
        # Typing flipped, or the chord itself rebound) takes effect without re-opening.
        'keys': _hotkeys().typing_keys(),
        'cycleKeys': _hotkeys().cycle_keys(),
        'rev': _rev[0],
    }
    if _resolved:
        spec.update({
            'operationResolved': _resolved.get('operation', ''),
            'operationAuto': _resolved.get('operationAuto', True),
            'inward': _resolved.get('inward', False),
            'flipped': _resolved.get('flipped', False),
            'component': _resolved.get('component', ''),
            'bodyNames': _resolved.get('bodyNames', []),
            'noTargets': _resolved.get('noTargets', False),
        })
    if _field_push:
        _field_seq[0] += 1
        spec['fields'] = dict(_field_push)
        spec['fieldsSeq'] = _field_seq[0]
        _field_push.clear()
    _send('/extrude/update', {'spec': spec})


# ── Extruding TO something clicked ───────────────────────────────────────────
# A click on anything that is not a profile (and not a whole body — those toggle the
# bodies a cut/join works on) says "extrude to THERE". Its position along the extrude
# axis is read — a face's plane, a vertex, an edge or sketch line's middle when it runs
# along the axis — and read back into the datum line that says it, with the parameters
# that govern it (plane_guess, the same search the plane box and the dimension box use).
# The Distance box then holds that line and Reach is "To plane", so the extrusion is
# parametric and stays on the reference. It is a reference, so it is never flexible.
_AXIS_INDEX = {'X': 0, 'Y': 1, 'Z': 2}
_LENGTH_UNITS = {'mm', 'cm', 'm', 'um', 'in', 'ft'}
_ref_sig = [None]
_field_push = {}          # fields the add-in sets for the panel: sent in _push_state
_field_seq = [int(time.time() * 1000)]   # bumped per push, so the panel applies each once


def _occurrence_for(design, comp, ent):
    if comp is None or comp == design.rootComponent:
        return None
    occ = getattr(ent, 'assemblyContext', None)
    while occ is not None:
        try:
            if occ.component == comp:
                return occ
            occ = occ.assemblyContext
        except Exception:
            break
    try:
        occ = design.activeOccurrence
        if occ is not None and occ.component == comp:
            return occ
    except Exception:
        pass
    try:
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        return occs.item(0) if occs and occs.count else None
    except Exception:
        return None


def _to_comp(occ):
    """world -> component frame, as a function on Point3D/Vector3D copies."""
    m = None
    if occ is not None:
        try:
            m = occ.transform2.copy()
            m.invert()
        except Exception:
            m = None

    def conv(g):
        g = g.copy()
        if m is not None:
            g.transformBy(m)
        return g
    return conv


def _reference_pick():
    """(entity, pick point) of a clicked reference not yet handled, or None."""
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            item = sel.item(i)
            ent = item.entity
            if _extrudable(ent) or adsk.fusion.BRepBody.cast(ent) is not None:
                continue
            try:
                tok = ent.entityToken
            except Exception:
                tok = repr(ent)
            p = item.point
            sig = (tok, round(p.x, 5), round(p.y, 5), round(p.z, 5))
            if sig == _ref_sig[0]:
                continue
            _ref_sig[0] = sig
            return ent, p
    except Exception:
        pass
    return None


def _ref_coordinate(ent, idx, conv):
    """(coordinate along axis `idx` in the component frame, native face or None,
    problem). Constant along the axis -> that value; otherwise the MIDDLE of what was
    clicked (an edge's midpoint, a side face's extent), as the plane box does."""
    def val(g):
        c = conv(g)
        return (c.x, c.y, c.z)[idx]

    def mid(vals):
        return (min(vals) + max(vals)) / 2.0

    try:
        face = adsk.fusion.BRepFace.cast(ent)
        if face is not None:
            plane = adsk.core.Plane.cast(face.geometry)
            if plane is not None:
                n = conv(plane.normal)
                if abs((n.x, n.y, n.z)[idx]) >= 0.99999 * n.length:
                    return val(plane.origin), (face.nativeObject or face), ''
            bb = face.boundingBox
            return mid([val(bb.minPoint), val(bb.maxPoint)]), None, ''
        if adsk.fusion.BRepVertex.cast(ent):
            return val(ent.geometry), None, ''
        edge = adsk.fusion.BRepEdge.cast(ent)
        if edge is not None:
            v = [val(edge.startVertex.geometry), val(edge.endVertex.geometry)]
            return (v[0] if abs(v[0] - v[1]) <= 1e-6 else mid(v)), None, ''
        if adsk.fusion.SketchPoint.cast(ent):
            return val(ent.worldGeometry), None, ''
        if adsk.fusion.SketchCurve.cast(ent):
            pts = []
            for a in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
                sp = getattr(ent, a, None)
                if sp is not None:
                    pts.append(val(sp.worldGeometry))
            if pts:
                return (pts[0] if max(pts) - min(pts) <= 1e-6 else mid(pts)), None, ''
        cp = adsk.fusion.ConstructionPlane.cast(ent)
        if cp is not None:
            plane = cp.geometry
            n = conv(plane.normal)
            if abs((n.x, n.y, n.z)[idx]) >= 0.99999 * n.length:
                return val(plane.origin), None, ''
            return None, None, "That plane isn't square to the extrusion."
        pt = adsk.fusion.ConstructionPoint.cast(ent)
        if pt is not None:
            return val(pt.geometry), None, ''
    except Exception:
        _log('reference coordinate failed:', traceback.format_exc().replace('\n', ' | '))
    return None, None, "That can't be extruded to — click a face, edge, point or line."


def _guess_datums(design, comp, axis):
    high, _low = bounds.high_low(axis)
    out = []
    for d in bounds.datum_words(design, comp):
        if d['axis'] != axis:
            continue
        if d['kind'] == 'midline':
            out.append(dict(d, coord=0.0))
            continue
        direction = d['datum'] if d['datum'] in bounds.DIRECTIONS else d.get('side', high)
        c = bounds.coord(design, comp, direction)
        if c is not None:
            out.append(dict(d, coord=c))
    return out


def _guess_params(design, comp, occ):
    own = set()
    try:
        own = {p.name for p, _k in bounds.bounds_parameters(design, comp)}
    except Exception:
        pass
    here = bounds.owner_level(design, comp) + '_'
    above = [bounds.owner_level(design, design.rootComponent) + '_']
    o = getattr(occ, 'assemblyContext', None) if occ is not None else None
    while o is not None:
        try:
            above.append(bounds.owner_level(design, o.component) + '_')
            o = o.assemblyContext
        except Exception:
            break
    out = []
    ups = design.userParameters
    for i in range(ups.count):
        p = ups.item(i)
        try:
            if p.unit not in _LENGTH_UNITS:
                continue
            name = p.name
            rank = (0 if name in own or name.startswith(here)
                    else 1 if any(name.startswith(a) for a in above) else 2)
            out.append({'name': name, 'value': p.value, 'rank': rank})
        except Exception:
            pass
    return out


def _face_provenance(design, comp, face, axis, target, datums):
    """([{text, note}], [hint terms]) from the extrude that made `face` — the same read
    Dimension Value's plane pick does: an end face is its sketch's line plus the extrude
    distance."""
    lines, hints = [], []
    idx = _AXIS_INDEX[axis]
    try:
        exts = face.body.parentComponent.features.extrudeFeatures
    except Exception:
        return lines, hints
    for i in range(exts.count):
        ext = exts.item(i)
        try:
            is_start = any(f == face for f in ext.startFaces)
            is_end = not is_start and any(f == face for f in ext.endFaces)
            if not (is_start or is_end):
                continue
            prof = ext.profile
            if adsk.core.ObjectCollection.cast(prof):
                prof = prof.item(0)
            sk = prof.parentSketch
        except Exception:
            continue
        dist = None
        if is_end:
            try:
                if not ext.hasTwoExtents:
                    dist = adsk.fusion.DistanceExtentDefinition.cast(ext.extentOne)
            except Exception:
                dist = None
            if dist is None:
                return lines, hints
            hints.append({'name': dist.distance.expression,
                          'value': abs(dist.distance.value), 'rank': -1, 'axes': []})
        text = bounds.sketch_datum_text(sk)
        if not text or sk.parentComponent != comp or bounds.sketch_axis(sk) != axis:
            return lines, hints
        word, rest = bounds.planes.split_input(text)
        d = next((x for x in datums if x['word'].lower() == word.lower()), None)
        if d is None:
            return lines, hints
        try:
            o = sk.transform.getAsCoordinateSystem()[0]
            pos = (o.x, o.y, o.z)[idx]
        except Exception:
            return lines, hints
        note = '%s of %s' % ('start' if is_start else 'end', ext.name)
        if is_start:
            if abs(pos - target) <= bounds.TOL:
                lines.append({'text': text, 'note': note})
        else:
            got = plane_guess.extend(d['word'], rest, plane_guess.rest_for(d, pos),
                                     dist.distance.expression, abs(dist.distance.value),
                                     plane_guess.rest_for(d, target))
            if got:
                lines.append({'text': got, 'note': note})
        return lines, hints
    return lines, hints


def _adopt_reference(design):
    """One tick of "extrude to what was clicked". True when the spec changed."""
    if not _picked or bounds is None:
        return False
    got = _reference_pick()
    if got is None:
        return False
    ent, pick_pt = got
    # Take the click back out of the selection: what is lit stays the profiles.
    _write_selection([] if _sel_hidden else _picked)
    # A FACE says "this body": it takes part in (or is left out of) the cut / join, the
    # same toggle a click on the body itself or its Browser row makes. Edges, vertices
    # and sketch lines / points are what the extrusion is measured TO.
    face = adsk.fusion.BRepFace.cast(ent)
    if face is not None:
        try:
            _toggle_bodies([face.body])
        except Exception:
            _log('face -> body toggle failed:', traceback.format_exc().replace('\n', ' | '))
        return False
    first = _picked[0]
    comp = _component_of(first)
    normal, point = _plane_of(first)
    axis, _sign = bounds._principal_axis(normal) if normal is not None else (None, None)
    if comp is None or not axis:
        _send('/extrude/error', {'error': 'The profile is not square to an axis, so there '
                                          'is no "to".'})
        return False
    idx = _AXIS_INDEX[axis]
    conv = _to_comp(_occurrence_for(design, comp, ent))
    target, face, problem = _ref_coordinate(ent, idx, conv)
    if problem:
        _send('/extrude/error', {'error': problem})
        return False

    um = design.unitsManager

    def fmt(v):
        try:
            return um.formatInternalValue(v, um.defaultLengthUnits, False)
        except Exception:
            return '%g' % v

    text = ''
    try:
        datums = _guess_datums(design, comp, axis) if plane_guess is not None else []
        if datums:
            prov, hints = ([], [])
            if face is not None:
                prov, hints = _face_provenance(design, comp, face, axis, target, datums)
            items = plane_guess.suggest(
                target, datums,
                hints + _guess_params(design, comp, _occurrence_for(design, comp, first)),
                provenance=prov, fmt=fmt)
            if items:
                text = items[0]['text']
    except Exception:
        _log('reference suggest failed:', traceback.format_exc().replace('\n', ' | '))
    if text:
        _spec['reach'] = 'to'
        _spec['distance'] = text
        _field_push.update({'reach': 'to', 'distance': text})
    else:
        # No bounds to describe it against: a plain signed length, from the profile.
        here = (point.x, point.y, point.z)[idx]
        d = (target - here) * (1 if _sign > 0 else -1)
        _spec['reach'] = 'distance'
        _spec['distance'] = um.formatInternalValue(d, um.defaultLengthUnits, True)
        _field_push.update({'reach': 'distance', 'distance': _spec['distance']})
    _log('reference ->', _spec['reach'], _spec['distance'])
    return True


def _adopt_picks():
    """Notice a fresh pick in the viewport. Main thread, on the tick.

    Only a NON-EMPTY selection is adopted. Fusion empties the ordinary selection as the
    command starts, and an empty set here would mean the pre-selection was thrown away in
    the first tick of every extrude.
    """
    global _picked, _picked_id
    picks = _viewport_picks()
    if not picks:
        return False
    now = _identity(picks)
    if _sel_hidden:
        # The picks are un-highlighted under a preview, so the selection holds only
        # what was clicked since — Fusion's click on an unlit profile SELECTS it. A
        # click is a toggle against the whole set, as natively: a profile already being
        # extruded comes out, a new one goes in. Then the selection is emptied again.
        ids = list(_identity(_picked))
        kept = list(_picked)
        for ent, i in zip(picks, now):
            if i in ids:
                at = ids.index(i)
                del ids[at], kept[at]
            else:
                ids.append(i)
                kept.append(ent)
        _picked = kept
        _picked_id = tuple(ids)
        _write_selection([])
        return True
    if now == _picked_id:
        return False
    _picked = picks
    _picked_id = now
    return True


def _write_selection(entities):
    """Make the viewport's selection exactly `entities`, as our own write: the
    inputChanged it may set off is not a pick (see _InputChanged)."""
    _sel_quiet_until[0] = time.time() + 0.3
    sel = ui.activeSelections
    try:
        sel.clear()
    except Exception:
        pass
    for e in entities:
        try:
            sel.add(e)
        except Exception:
            pass
    _sel_quiet_until[0] = time.time() + 0.3


def _sync_highlight():
    """Un-highlight the picks while a preview stands; light them again when it goes.
    See _sel_hidden."""
    global _sel_hidden, _picked_id
    want = bool(_running and _cmd is not None and _previewed and _picked
                and _spec.get('preview', True) and not _applying)
    if want == _sel_hidden:
        return
    _sel_hidden = want
    _write_selection([] if want else _picked)
    if not want:
        # What the tick will read back, so it does not "adopt" our own write.
        _picked_id = _identity(_viewport_picks())
    _log('picks', 'hidden under the preview' if want else 'lit again')


def _clear_selection():
    """Throw away what is being extruded, from the panel's Clear.

    Both halves of it: the add-in's own copy (which is what _build reads — see
    _selected_entities) and Fusion's live selection, which is what you can SEE lit up in
    the viewport. Clearing one without the other leaves a panel that says nothing is
    picked over a face that is still blue, or the reverse.

    Nothing re-adopts an empty selection afterwards (_adopt_picks ignores one on
    purpose), so this holds until the next click.
    """
    global _picked, _picked_id, _sel_hidden
    _picked = []
    _picked_id = ()
    _sel_hidden = False          # nothing left to light again
    _clear_ghost()
    # The redraw goes FIRST, while everything the running command holds is still valid.
    # _build now has nothing to build, so this is the call that rolls the last preview
    # off the model — and doing it after clearing Fusion's selection meant asking the
    # command to redraw against references that had just been pulled out from under it,
    # which it answers with "an API object refers to a deleted object".
    _preview()
    try:
        ui.activeSelections.clear()
    except Exception:
        pass
    try:
        if _sel_input is not None:
            _sel_input.clearSelection()
    except Exception:
        pass
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is not None:
        _open_panel(design)


def _pick_sketches(sketches, toggle):
    """Extrude every profile of these sketches — what a sketch clicked in the Browser
    window means while this command is open. Called through HOOK, on the main thread.

    Picks add up, the way viewport clicks do under a running command. With `toggle` (a
    plain click on one sketch) a sketch whose profiles are ALL already picked comes back
    out instead; a multi-select only ever adds.

    Returns False when no extrude is running, so the Browser selects as usual.
    """
    global _picked, _picked_id, _sel_hidden
    if not _running or _cmd is None:
        return False
    if not sketches:
        return True         # a body or a group: nothing to extrude, picks stand
    try:
        picks = list(_picked)
        ids = list(_identity(picks))
        for sk in sketches:
            try:
                profs = [sk.profiles.item(i) for i in range(sk.profiles.count)]
            except Exception:
                continue
            if not profs:
                continue
            pids = _identity(profs)
            if toggle and all(p in ids for p in pids):
                kept = [(e, i) for e, i in zip(picks, ids) if i not in pids]
                picks = [e for e, _ in kept]
                ids = [i for _, i in kept]
            else:
                for p, i in zip(profs, pids):
                    if i not in ids:
                        picks.append(p)
                        ids.append(i)
        if not picks:
            _clear_selection()
            return True
        _picked = picks
        # The viewport shows what is being extruded, the same as a click there would —
        # until the preview below stands, which un-lights it again (_sync_highlight).
        _write_selection(picks)
        _sel_hidden = False
        # What the tick will read back, so it does not "adopt" our own write.
        _picked_id = _identity(_viewport_picks())
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is not None:
            _open_panel(design)
            _preview()
    except Exception:
        _log('pick sketches failed:', traceback.format_exc().replace('\n', ' | '))
    return True


def _apply_panel_commands():
    """Act on whatever the poller has fetched. Main thread, inside the custom event."""
    with _box_lock:
        commands = _inbox[:]
        del _inbox[:]
    if not commands:
        return
    changed = False
    chose = False
    was_preview = _spec.get('preview', True)
    for c in commands:
        action = c.get('action')
        # Before acting on it, so every push from here on says this one was heard.
        if isinstance(c.get('rev'), (int, float)):
            _rev[0] = max(_rev[0], int(c['rev']))
        if action == 'setextrude':
            scope_was = _spec.get('bodiesScope')
            for key in _DEFAULT_SPEC:
                if key in c and c[key] != _spec.get(key):
                    if key in _CHOICES:
                        chose = True
                    _spec[key] = c[key]
                    changed = True
            # The panel only ever sends the list when a chip or its Clear was clicked.
            if 'exclude' in c or 'groups' in c:
                _spec['bodiesAuto'] = False
                chose = changed = True
            if _spec.get('bodiesScope') != scope_was:
                if _spec.get('bodiesScope') == 'group':
                    _seed_groups()
                chose = changed = True
            # Nothing different is nothing to rebuild: the panel's chip layer re-fires
            # `input` as it opens, which previewed a whole edit a second time.
        elif action == 'clearextrude':
            _clear_selection()
            continue
        elif action == 'makeparam':
            _make_param(c)
            continue
        elif action == 'editmade':
            _edit_made(c)
            continue
        elif action == 'commitextrude':
            _commit()
            return
        elif action == 'cancelextrude':
            _cancel()
            return
    if not changed:
        return
    if _spec.get('preview', True):
        if _same_as_built():
            _push_state()        # the rev still has to go back
        else:
            _preview()
    elif was_preview:
        _unpreview()
    elif chose:
        _choose()
    else:
        _push_state()


def _make_param(c):
    """The panel's "Make parameter". Only remembered here — written by the next preview
    (the panel puts the name in the field, which previews) and by execute; a write now
    would be rolled back by that preview. See Made Params.py."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return
    why = _made.take(design, c)
    if why:
        _send('/extrude/error', {'error': why})
        return
    _send('/made', {'name': _made.items[-1]['name'],
                    'params': _made.params(design, _params_snapshot(design))})


def _edit_made(c):
    """The panel's "Edit parameter" (a parameter chip's menu). Remembered like a made
    parameter — written by the next preview and by execute (Made Params.py)."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return
    why = _made.take_edit(design, c)
    if why:
        _send('/extrude/error', {'error': why})
        return
    e = _made.edits[-1]
    _send('/made', {'name': e['newName'] or e['name'],
                    'edited': {'old': e['name'], 'new': e['newName']},
                    'params': _made.params(design, _params_snapshot(design))})


# Rows of the panel that are a choice rather than typed text. With preview off, an edit
# takes one of these the moment it is clicked; typed text waits for Enter.
_CHOICES = ('direction', 'operation', 'isSolid', 'start', 'reach')


def _unpreview():
    """Preview just switched off: a preview that builds nothing takes the last one away.
    Fusion answers an empty preview after a real one with "refers to a deleted Object"
    from doExecutePreview — the command carries on regardless (measured), so it is not
    logged as a failure."""
    if _cmd is None:
        return
    try:
        _cmd.doExecutePreview()
        app.activeViewport.refresh()
    except Exception:
        pass
    _push_state()


def _choose():
    """A choice changed (a click, or a cycle key): preview it, or — preview off, on an
    edit — apply it to the feature now. A new extrude has nothing to apply to yet."""
    if _spec.get('preview', True) or _edit_target() is None:
        _preview()
    else:
        _apply()


def _unchanged_edit():
    """An edit whose panel still says exactly what the feature does."""
    seed = _edit.get('seed') or {}
    return (_edit_target() is not None
            and _identity(_selected_entities()) == _edit.get('profiles')
            and all(str(_spec.get(k) or '').strip() == str(seed.get(k) or '').strip()
                    for k in _DEFAULT_SPEC if k not in ('preview', 'bodiesAuto')))


def _apply():
    """Run execute WITHOUT ending the command: the panel's state goes into the model for
    real. Preview off only. True if it built; if not the panel is told why and the
    transaction was aborted (_Execute), so nothing half-applied stands.

    An edit carries on against what is now in the feature: the seed moves up to what
    was applied, so the next apply writes only what changes after this one."""
    global _applying, _applied
    if _cmd is None:
        return False
    if _unchanged_edit():
        return True
    _applying, _applied = True, None
    try:
        _cmd.doExecute(False)
    except Exception:
        _log('apply failed:', traceback.format_exc().replace('\n', ' | '))
    finally:
        _applying = False
    made, _applied = _applied, None
    if made is None:
        _send('/extrude/error',
              {'error': ('No bodies to work on — click one to add it.'
                         if _resolved.get('noTargets') else
                         'That extrude will not build — check the distance.')})
        _push_state()
        return False
    if _edit_target() is not None:
        _edit['seed'] = dict(_spec)
        _edit['profiles'] = _identity(_selected_entities())
    _push_state()
    return True


def _preview():
    """Redraw. doExecutePreview is the whole reason this is a skin rather than a rewrite:
    it fires the command's own preview, so the viewport shows the real feature."""
    if _cmd is None:
        return
    try:
        _cmd.doExecutePreview()
        # The preview genuinely modifies the model (isValidResult is True), but the
        # viewport does not always repaint for a change it did not originate — and a cut
        # that is not repainted looks exactly like a cut that did not happen.
        try:
            app.activeViewport.refresh()
        except Exception:
            pass
        _push_state()
        _sync_highlight()
    except Exception:
        _log('preview failed:', traceback.format_exc().replace('\n', ' | '))


# ── Half-typed expressions ───────────────────────────────────────────────────
# The panel posts on every keystroke, so most of what arrives on the way to "Width + 10"
# is not an expression: "Width +", "Width + 1" is, "Wid" is not. Built as typed, each of
# those took the preview away and the next brought it back — the viewport flashed with
# every key. A field that is not a whole expression now previews as its last text that
# was (_last_good), so the preview holds and moves only when the text is viable again.
# OK still goes by what is TYPED: _commit refuses while a field is half typed.

_FIELD_WORDS = {'distance': 'distance', 'distance2': 'other side',
                'startOffset': 'start offset', 'taper': 'taper angle'}

# The fields the standing preview stood in for (_viable_spec swapped them), or [].
_half_typed = []


def _viable_spec(design):
    """_spec as a preview builds it: each half-typed expression field replaced by its
    last whole text. Keeps _last_good current on the way."""
    out = dict(_spec)
    for key in _EXPR_FIELDS:
        text = (_spec.get(key) or '').strip()
        if not text:
            _last_good.pop(key, None)
            continue
        # Whole in ANY unit: the question is "finished?", not "the right unit?" — a
        # unit that is wrong is the build's to refuse, as before.
        try:
            if key == 'distance' and _spec.get('reach') == 'to':
                ent = _picked[0] if _picked else None
                comp = _component_of(ent) if ent is not None else None
                whole = (ent is None or
                         _to_distance(design, comp, ent, text)[1] == '')
            else:
                whole = madeparams.infer_unit(design, text) is not None
        except Exception:
            whole = True             # cannot tell: let the build say
        if whole:
            _last_good[key] = text
        elif key in _last_good:
            out[key] = _last_good[key]
    return out


def _spec_key(spec):
    return json.dumps({k: spec.get(k) for k in _DEFAULT_SPEC},
                      sort_keys=True, default=str) + repr(_picked_id)


def _same_as_built():
    """Would a preview now build exactly what is standing? Never while the panel has
    made a parameter: those exist only inside a preview (Made Params.py), so out here a
    field naming one reads as half typed, and would be taken for "nothing changed"."""
    if not _previewed or _built_key[0] is None or _made.items or _made.edits:
        return False
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return False
    return _spec_key(_viable_spec(design)) == _built_key[0]


def _commit():
    """Take the previewed feature as the result.

    A fresh preview first, and only commit if it SUCCEEDED — not to make the result (the
    preview is rolled back and `execute` builds the real one), but to find out whether it
    CAN be made while the panel is still up to say so. Committing something that will not
    build otherwise ends the command having silently produced nothing.
    """
    if _cmd is None:
        return
    if not _spec.get('preview', True):
        # Nothing was previewed to check against. Apply without ending the command,
        # so a build that fails leaves the panel up to say so; then end it.
        if _apply():
            _cancel()
        return
    try:
        _cmd.doExecutePreview()
    except Exception:
        _log('commit preview failed:', traceback.format_exc().replace('\n', ' | '))
    if not _previewed:
        _send('/extrude/error',
              {'error': ('No bodies to work on — click one to add it.'
                         if _resolved.get('noTargets') else
                         'That extrude will not build — check the distance.')})
        return
    if _half_typed:
        # It previewed, but as the last whole text of a field that is not one now — and
        # execute builds what is typed. See "Half-typed expressions".
        _send('/extrude/error',
              {'error': 'The %s is not finished.' % _FIELD_WORDS.get(_half_typed[0],
                                                                    _half_typed[0])})
        return
    try:
        _cmd.doExecute(True)
    except Exception:
        _log('commit failed:', traceback.format_exc().replace('\n', ' | '))


def _cancel():
    """End the command without building anything.

    doExecute(terminate=True) with the builder switched off. That is a strange-looking
    way to cancel, and it is the only one that works — both of the obvious routes were
    tried and measured:

      - SelectCommand, to pre-empt it: returns cleanly, command keeps running.
      - ui.terminateActiveCommand(): returns TRUE, command keeps running.

    Both fail for the same reason: with its palette hidden this command is not Fusion's
    "active command", so nothing aimed at the active command reaches it. doExecute is
    aimed at THIS command object, which is why it lands — it is how OK works.

    Nothing is built, so nothing is committed; every preview has already been rolled
    back. Leaving this broken meant a panel you could close over a command you could
    not, with no palette on screen to escape from — the worst state this tool has.
    """
    global _cancelling
    if _cmd is None:
        return
    _cancelling = True
    try:
        _cmd.doExecute(True)
    except Exception:
        _log('cancel failed:', traceback.format_exc().replace('\n', ' | '))
    finally:
        _cancelling = False


# ── The command ──────────────────────────────────────────────────────────────

class _ExecutePreview(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _previewed, _preview_runs
        # Cleared FIRST: a preview that fails (a half-typed expression, a distance that
        # no longer cuts anything) must not leave the previous one's flag standing, or OK
        # would keep a result the model no longer holds.
        _previewed = False
        _built_key[0] = None
        _half_typed[:] = []
        if time.time() - _reactivated[0] < 2.0:
            _log('executePreview fired by Fusion after a re-activate')
        # The panel's made parameters, written again after the last preview's rollback
        # took them — see Made Params.py. First, so the distance can name them.
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is not None:
                _made.ensure(design)
        except Exception:
            pass
        if not _spec.get('preview', True):
            # Preview off: build nothing, which also takes the last preview away.
            _clear_ghost()
            return
        _preview_runs += 1
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                return
            if _editing is not None:
                _settle_direction(design, may_roll=True)
            # Half-typed fields build as their last whole text, for this build only —
            # see "Half-typed expressions". After _settle_direction, which may rewrite
            # the distance itself.
            viable = _viable_spec(design)
            swapped = {k: _spec.get(k) for k in _EXPR_FIELDS
                       if viable.get(k) != _spec.get(k)}
            _spec.update({k: viable[k] for k in swapped})
            began = time.time()
            try:
                built = _build(design)
            finally:
                for k, typed in swapped.items():
                    if _spec.get(k) == viable[k]:
                        _spec[k] = typed
            if _editing is not None:
                _note_build(time.time() - began)
            _half_typed[:] = list(swapped)
            _built_key[0] = _spec_key(viable) if built is not None else None
            if built is None:
                return
            # FALSE, and it has to be. True — "the previewed feature IS the result, leave
            # it standing" — looks like the way to make a cut visible and breaks three
            # things, all of them observed rather than feared:
            #
            #   1. A cut standing in the model DESTROYS the face it was cut from, so the
            #      next preview builds against a dead reference and dies with "invalid
            #      argument profile". Extruding a body's own face never got past its
            #      second keystroke.
            #   2. Cancel stopped being a cancel. Pre-empting the command left the last
            #      preview's feature behind as a real timeline entry, so Escape silently
            #      committed a cut.
            #   3. doExecute fires execute anyway, so OK built the feature a second time,
            #      on top of a model the first one had already changed.
            #
            # False gives the ordinary contract: Fusion shows the computed result, then
            # rolls it back, so references stay alive, Escape undoes and execute is the
            # one and only place a feature is really made. What was missing for cuts was
            # never this flag — it was the viewport repaint, see _preview.
            args.isValidResult = False
            _previewed = True
        except Exception:
            _log('preview build failed:', traceback.format_exc().replace('\n', ' | '))


class _Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        """The one and only place a feature is really made.

        Every preview is rolled back (see _ExecutePreview), so this builds from scratch —
        same call, same inputs, same result as the last thing you were shown.
        """
        global _applied, _numbers
        design = adsk.fusion.Design.cast(app.activeProduct)
        # Before the cancel's return: a parameter made from the panel stays made.
        if design is not None:
            _made.ensure(design)
            _made.finish_edits()
        if _cancelling:
            return          # ending the command, not committing it — see _cancel
        if design is not None:
            _write_flex(design)
        made = None
        try:
            if design is not None:
                began = time.time()
                # Typed numbers become number parameters in this build (see _value);
                # an edited feature's own ones are reused, not left behind.
                _numbers = {'old': numbers.old_numbers(design, numbers.feature_text(_editing))
                            if _editing is not None else {}, 'made': []}
                made = _build(design, committing=True)
                if _editing is not None:
                    _note_build(time.time() - began)
        except Exception:
            _log('execute failed:', traceback.format_exc().replace('\n', ' | '))
        finally:
            if _numbers is not None and design is not None:
                # The edited feature's old ones no longer used, and any made but not
                # used (a failed build) — drop() spares whatever is still named.
                numbers.drop(design, list(_numbers['old']) + _numbers['made'])
            _numbers = None
        if _applying:
            _applied = made
            if made is None:
                # An edit that will not compute has already been written into the
                # feature; abort the transaction so it does not stand.
                args.executeFailed = True


class _InputChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        """A pick in the viewport. The selection is the one input the panel does not own,
        so this is where the panel finds out what you clicked."""
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                return
            if time.time() < _sel_quiet_until[0]:
                # Our own selection write (_write_selection) reported back — the picks
                # un-lit under a preview or lit again. Not a pick; nothing to rebuild.
                now = _identity(_viewport_picks())
                if now == (() if _sel_hidden else _picked_id):
                    return
            _open_panel(design)
            _preview()
        except Exception:
            _log('inputChanged failed:', traceback.format_exc().replace('\n', ' | '))


class _Activate(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _running
        try:
            _running = True
            _hide_palette()
            if _opened[0]:
                # Not a start: Fusion re-activates a command after every view operation
                # (orbit, zoom, pan) it ran nested inside it. The panel is already up and
                # the preview already built — re-opening re-armed the panel (its field
                # re-selected, the chip opened as text) and rebuilding flashed the body.
                _log('re-activated (view op) — panel and preview left alone')
                _reactivated[0] = time.time()
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                return
            if _editing is not None:
                # Settled FIRST — it can negate the distance, and the panel must open on
                # the settled text. Here without rolling; a preview may roll if it must.
                # Fusion has usually previewed once already by now (and settled there):
                # a second one would build the whole edit again for nothing.
                _settle_direction(design, may_roll=False)
                if _spec.get('preview', True) and not _preview_runs:
                    _preview()
                _open_panel(design)
                return
            _open_panel(design)
            if _selected_entities():
                _preview()
        except Exception:
            _log('activate failed:', traceback.format_exc().replace('\n', ' | '))


class _Destroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _cmd, _sel_input, _running, _picked, _picked_id, _editing, _edit
        global _sel_hidden
        _running = False
        _sel_hidden = False
        _choose_after[0] = None
        _cmd = None
        _opened[0] = False
        _sel_input = None
        _picked = []
        _picked_id = ()
        _editing = None
        _edit = {}
        # Nothing rolls custom graphics back, so leaving without this paints the ghost
        # over the model for good.
        _clear_ghost()
        # The panel is this command's dialog, so it goes when the command does — however
        # the command ended, including Escape in the viewport.
        _send('/cmd', {'action': 'cancel'})


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _cmd, _sel_input, _spec, _resolved, _previewed, _picked, _picked_id
        global _preview_runs, _sel_hidden
        try:
            if self not in handlers:
                return
            cmd = args.command
            _cmd = cmd
            _opened[0] = False
            _spec = dict(_DEFAULT_SPEC)
            _resolved = {}
            _previewed = False
            _preview_runs = 0
            _made.reset()
            _rev[0] = 0
            _last_good.clear()
            _built_key[0] = None
            _half_typed[:] = []
            _choose_after[0] = None
            _sel_hidden = False

            # No auto-execute and no OK button: the panel's Enter is the OK, and it comes
            # back through doExecute.
            cmd.isAutoExecute = False
            cmd.isExecutedWhenPreEmpted = False
            try:
                cmd.isOKButtonVisible = False
            except Exception:
                pass
            # Shrink what is left before it is drawn, so the frame _hide_palette then
            # takes away never had a size worth flashing.
            try:
                cmd.setDialogSize(1, 1)
                cmd.setDialogMinimumSize(1, 1)
            except Exception:
                pass

            inputs = cmd.commandInputs
            sel = inputs.addSelectionInput('extrudeSelection', 'Profile',
                                           'Profile, or a body to work on')
            # Profiles, and BODIES — but a body is never what gets extruded. A click on
            # one puts it in or takes it out of what the join/cut/intersect works on
            # (_adopt_body_clicks), and is taken straight back out of the selection.
            # No planar faces: with bodies pickable a click on a face means its body,
            # and extruding a face is not how this is used — sketches only.
            for f in ('Profiles', 'SolidBodies'):
                try:
                    sel.addSelectionFilter(f)
                except Exception:
                    pass
            sel.setSelectionLimits(0, 0)
            _sel_input = sel

            # Whatever was picked before the key was pressed, captured NOW: Fusion
            # clears the ordinary selection as the command takes over, so this is the
            # last moment it can be read.
            _picked = _viewport_picks()
            _picked_id = _identity(_picked)

            # An edit: handed over by _EditStarting, or an extrude selected when this
            # command was started directly. Either way the panel opens on the feature.
            _begin_edit()

            # Into _cmd_handlers, NOT handlers. These five belong to THIS command and die
            # with it, whereas `handlers` is the add-in's own registration and is what
            # stop() unregisters. Appending them there grew it by five on every single
            # extrude — 27 entries after five runs — keeping every dead command's
            # handlers alive for the life of the session.
            del _cmd_handlers[:]
            for evt, handler in ((cmd.activate, _Activate()),
                                 (cmd.executePreview, _ExecutePreview()),
                                 (cmd.execute, _Execute()),
                                 (cmd.inputChanged, _InputChanged()),
                                 (cmd.destroy, _Destroy())):
                evt.add(handler)
                _cmd_handlers.append(handler)
        except Exception:
            _log('created failed:', traceback.format_exc().replace('\n', ' | '))


# ── The tick ─────────────────────────────────────────────────────────────────

class _Tick(adsk.core.CustomEventHandler):
    def notify(self, args):
        if not _running:
            return
        try:
            # Bodies first: they leave the selection here, so the profile read below
            # sees only what is being extruded.
            _adopt_body_clicks()
            if _adopt_picks():
                design = adsk.fusion.Design.cast(app.activeProduct)
                if design is not None:
                    _open_panel(design)
                    _preview()
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is not None and _adopt_reference(design):
                _preview()
                _push_state()
            _apply_panel_commands()
            _run_deferred_choose()
            # Every tick, not only after our own previews: Fusion fires some itself
            # (after activate, after an input change), and a flag compare is free.
            _sync_highlight()
        except Exception:
            _log('tick failed:', traceback.format_exc().replace('\n', ' | '))


# Set while a heal is in flight, so a window that takes a couple of seconds to come up
# is not asked to heal again on every tick of that wait.
_healing_until = 0.0


def _heal_window():
    global _healing_until
    if time.time() < _healing_until:
        return
    _healing_until = time.time() + 6.0
    _post('/cmd', {'action': 'ensurewindow'})


# When the panel was first seen NOT showing this command's session, or 0.
_lost = [0.0]
_LOST_SECS = 1.5
# Set once this command has asked the panel to open (_open_panel). Until then there is
# no panel to have lost: an edit's first preview can hold the main thread for many
# seconds (_settle_direction rolls a big timeline back and forward) BEFORE the open is
# sent, and a beat counted through that queued a cancel that landed the moment the
# panel finally appeared — it opened for half a second and shut.
_opened = [False]
# When the command last came back from a nested view op — see _Activate. Diagnostic.
_reactivated = [0.0]


def _watch_panel(got, action):
    """End the command once the window has stopped showing its panel for a beat.

    The command runs hidden behind the panel, so it can only be ended from there. But
    the panel can be replaced without ending it — Command Search, a value box, a
    relaunch — and then an invisible command stays running: Fusion reads as busy and
    every idle-only shortcut goes dead until some other command happens to run. The
    beat covers the moment between activate and the panel's open arriving. A window
    that is not answering is left to the heal instead — its session comes back."""
    if (not _opened[0] or not got or 'showing' not in got
            or not got.get('windowAlive', True)):
        _lost[0] = 0.0
        return
    if got.get('showing'):
        _lost[0] = 0.0
        return
    now = time.time()
    if not _lost[0]:
        _lost[0] = now
    elif now - _lost[0] > _LOST_SECS:
        _lost[0] = 0.0
        _log('panel gone (window shows %r) — ending the hidden command'
             % (got.get('mode') or 'nothing'))
        with _box_lock:
            _inbox.append({'action': action})


def _poller(stop):
    """All of the network I/O, and the only thing that fires the tick.

    It touches no Fusion state beyond fireCustomEvent — see fusion_bg_thread_qt_crash for
    why that line matters — and it fires the event only when there is something to apply,
    so an open panel nobody is typing into costs nothing at all.
    """
    while not stop.is_set():
        try:
            # ALWAYS, even once the command has gone: the last thing a finished extrude
            # puts in the outbox is the cancel that closes the panel, and by then
            # _running is already False. Gating this on it left the panel up for ever.
            with _box_lock:
                pending = _outbox[:]
                del _outbox[:]
            for path, payload in pending:
                _post(path, payload)
                _sent[0] += 1
                _sent_at[0] = time.time()

            if _running:
                got = _get('/extrude', timeout=1)
                commands = (got or {}).get('commands') or []
                if commands:
                    with _box_lock:
                        _inbox.extend(commands)
                # Fired EVERY cycle while the panel is open, not only when the panel has
                # said something: the tick is also what notices a pick in the viewport,
                # and nothing tells us about those. Only while a command is running, so
                # an idle Fusion still costs nothing.
                app.fireCustomEvent(_EVENT_ID, '')

                # Only when the window really is not answering. Dimension Value owns
                # that process and this add-in can only ask — but asking is expensive:
                # the heal may pkill and relaunch the app, and the wait for that runs on
                # Fusion's MAIN thread, which is long enough to tear down the very
                # command this panel is steering. Asking unconditionally on every
                # extrude made the command die on arrival, intermittently, depending on
                # whether the window happened to be polling. So it is asked once, only
                # when the answer is no.
                if got is not None and not got.get('windowAlive', True):
                    _heal_window()
                _watch_panel(got, 'cancelextrude')
            else:
                _lost[0] = 0.0
        except Exception:
            pass
        stop.wait(_TICK_SECS)


# ── Lifecycle ────────────────────────────────────────────────────────────────

def run(context):
    global _created, _custom_evt, _sync, _poller_stop, _starting, _edit_evt, _edit_launch
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            # Three arguments, not four: passing '' as the resource folder makes a
            # definition that registers and reports isValid but will not launch.
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)
        else:
            try:
                if cmd_def.name != CMD_NAME:
                    cmd_def.name = CMD_NAME
                cmd_def.tooltip = CMD_DESC
            except Exception:
                pass

        created = _CommandCreated()
        cmd_def.commandCreated.add(created)
        handlers.append(created)
        _created = created

        try:
            app.unregisterCustomEvent(_EVENT_ID)
        except Exception:
            pass
        _custom_evt = app.registerCustomEvent(_EVENT_ID)
        sync = _Tick()
        _custom_evt.add(sync)
        handlers.append(sync)
        _sync = sync

        # Edit Feature on an extrude opens this panel — see "Editing an existing extrude".
        try:
            app.unregisterCustomEvent(_EDIT_EVENT_ID)
        except Exception:
            pass
        _edit_evt = app.registerCustomEvent(_EDIT_EVENT_ID)
        _edit_launch = _EditLaunch()
        _edit_evt.add(_edit_launch)
        handlers.append(_edit_launch)
        _starting = _EditStarting()
        ui.commandStarting.add(_starting)
        handlers.append(_starting)

        _poller_stop = threading.Event()
        threading.Thread(target=_poller, args=(_poller_stop,), daemon=True).start()

        # A control is what puts the command in Fusion's own keyboard-shortcut list,
        # which is the only keymap in this setup.
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        try:
            _declare_actions()
        except Exception:
            _log('could not declare actions:', traceback.format_exc().replace('\n', ' | '))

        sys.modules[HOOK] = types.SimpleNamespace(
            pick_sketches=lambda sketches, toggle=False: _pick_sketches(sketches, toggle),
            pick_bodies=lambda bodies, group=None: _pick_bodies(bodies, group),
            running=lambda: bool(_running and _cmd is not None),
            state=lambda: _hook_state())

        global _group_watch
        _group_watch = groups.Watcher('ExtrudeGroupSync', _sync_groups, _log, _groups_busy)
        _group_watch.start()
    except Exception:
        ui.messageBox('Extrude run() failed:\n%s' % traceback.format_exc())


_group_watch = None


def stop(context):
    """Unregister BEFORE dropping the references — see _created."""
    global _created, _custom_evt, _sync, _poller_stop, _cmd, _sel_input, _running
    global _starting, _edit_evt, _edit_launch, _group_watch
    sys.modules.pop(HOOK, None)
    try:
        if _group_watch is not None:
            _group_watch.stop()
    except Exception:
        pass
    _group_watch = None
    try:
        # End a command that is still running BEFORE dropping the references to it.
        # Reloading this add-in while an extrude is open otherwise orphans it: the new
        # module's handlers are not attached to the old command, so nothing answers the
        # panel, cancel does nothing, and the only way out is Escape in the viewport —
        # with the palette hidden there is not even anything on screen to aim at.
        if _running and _cmd is not None:
            _cancel()
        _clear_ghost()
        try:
            _post('/cmd', {'action': 'cancel'})
        except Exception:
            pass

        if _poller_stop is not None:
            _poller_stop.set()
        _poller_stop = None
        _running = False
        _cmd = None
        _sel_input = None

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is not None and _created is not None:
            try:
                cmd_def.commandCreated.remove(_created)
            except Exception:
                pass
        _created = None
        if _custom_evt is not None and _sync is not None:
            try:
                _custom_evt.remove(_sync)
            except Exception:
                pass
        _sync = None
        _custom_evt = None
        try:
            app.unregisterCustomEvent(_EVENT_ID)
        except Exception:
            pass
        if _starting is not None:
            try:
                ui.commandStarting.remove(_starting)
            except Exception:
                pass
        _starting = None
        if _edit_evt is not None and _edit_launch is not None:
            try:
                _edit_evt.remove(_edit_launch)
            except Exception:
                pass
        _edit_evt = None
        _edit_launch = None
        try:
            app.unregisterCustomEvent(_EDIT_EVENT_ID)
        except Exception:
            pass

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        # The definition itself is left in place: deleting it would drop the key binding
        # with it. See the reload/shortcut-safe pattern the other add-ins use.
        try:
            _hotkeys().withdraw('Extrude')
        except Exception:
            pass
        handlers.clear()
        del _cmd_handlers[:]
    except Exception:
        pass
