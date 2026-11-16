"""
Mirror — the native feature, wearing the value window's face.

The Extrude add-in's arrangement, applied to Mirror: a command of OUR own runs with its
palette hidden, every change rebuilds a real MirrorFeature in executePreview, and the
panel in the value window steers it. Read Extrude.py's docstring for why it is built
this way; only what is different is said here.

WHAT IT ADDS
────────────
  THE COMPONENT'S OWN ORIGIN.  The plane is X, Y or Z of the origin of the component the
  mirrored bodies are IN — not the root's, not the active component's. X means across X:
  the component's YZ plane. X is the default, and the shared Cycle Direction action (one
  key for every command with a direction) steps X → Y → Z → the picked plane.

  ITS OWN COMPONENT.  The feature is made in that same component, whichever one is
  active, for the reason Extrude gives: working on something you can see from a level up
  should not first require going down to it.

  A PLANE OF YOUR OWN.  "Pick" on the panel arms a pick: the next planar face or
  construction plane clicked is the plane. A construction plane clicked at any time is
  taken as the plane without arming — it can never be a thing to mirror. A pick of the
  component's own origin plane is just X, Y or Z again. A plane in ANOTHER component is
  refused: a feature referencing a sibling's geometry is the cross-component link this
  whole scheme exists to avoid.

  RUN WITH SOMETHING SELECTED, IT JUST MIRRORS.  Bodies or features already selected when
  the command starts are mirrored at once with the defaults (X, Join) — no panel. The
  panel is for starting with nothing selected, or for a default mirror that won't build.

  CLICKING A FACE MIRRORS ITS BODY.  Bodies and features are what gets mirrored; a face
  click (the ordinary selection priority) is taken as its body.

  A GROUP, OR THE WHOLE COMPONENT.  "Bodies are: Group" mirrors every body (of the
  component) in the Browser groups of the bodies clicked — a chips row lists the
  component's groups to toggle. "Bodies are: Component" needs no click at all: every
  body of the component the clicked bodies are in, or of the active component when
  nothing is clicked. Either way the mirror keeps following once made, as Extrude's and
  Combine's do (see "Following a group or component").

THE PANEL HAS NO TEXT FIELD
───────────────────────────
Every row is a choice, so the panel holds the keyboard with nothing to type into. That
means Fusion's keymap sees nothing while it is up — so the panel hands every chord that
means something right now back to the router (hotkeys.py::panel_keys / press(any_key)),
which is how the Cycle Direction key still reaches us. See fusion_value/index.js, "The Mirror
panel".

NOT HERE (YET)
──────────────
Edit Feature on a mirror still opens Fusion's own dialog — Extrude's edit takeover has no
counterpart here. Components (occurrences) cannot be mirrored from this panel.
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


# Browser groups and the live-set record (Groups.py, shared with Combine and Extrude).
groups = _load('groups', 'Groups.py')
tickguard = _load('tickguard', 'Tick Guard.py')

CMD_ID = 'SmartMirrorCmd'
CMD_NAME = 'Mirror'
CMD_DESC = ('Mirror the selected bodies or features across the X, Y or Z origin plane of '
            'the component they are in — X unless you choose otherwise — or across a '
            'face or plane you pick.')
PANEL_ID = 'SolidScriptsAddinsPanel'

# ── Actions ───────────────────────────────────────────────────────────────────
# See Extrude.py's "Actions": this provides the shared Cycle Direction (the plane) and
# Cycle Operation while a mirror is running.
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'

# The panel's own order. 'picked' joins the ring only once there is a picked plane to go
# back to — cycling onto an empty pick would be a press that mirrors across nothing.
_PLANE_CYCLE = ('x', 'y', 'z', 'picked')


def _hotkeys():
    """The shared action registry — Extrude.py's _hotkeys, for the same reasons (ONE
    registry via sys.modules, and hot-reload on the file's mtime)."""
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


def _cycle_plane(d=1):
    """Step the plane on one (d=-1: back one), and redraw. Runs from the router inside
    the live command, same as Extrude's cycles — nothing here touches the command
    system."""
    if not _running or _cmd is None:
        return
    ring = [p for p in _PLANE_CYCLE if p != 'picked' or _plane_ent is not None]
    current = _spec.get('plane', 'x')
    try:
        nxt = ring[(ring.index(current) + d) % len(ring)]
    except ValueError:
        nxt = ring[0]
    _spec['plane'] = nxt
    _log('cycle plane', current, '->', nxt)
    _arm_filter()
    _defer_preview()


def _cycle_operation():
    """Join ↔ new body. Bodies only: a mirror of features has no operation."""
    if not _running or _cmd is None or _kind_now() != 'bodies':
        return
    current = _spec.get('operation', 'join')
    nxt = 'newbody' if current == 'join' else 'join'
    _spec['operation'] = nxt
    _log('cycle operation', current, '->', nxt)
    _defer_preview()


def _defer_preview():
    """The row first, the viewport after — Extrude's _defer_choose: a preview holds the
    poller too, so the panel heard about the key only once the viewport showed it. A key
    pressed IN the panel never comes here (the panel steps the row itself)."""
    _push_state()
    with _box_lock:
        _preview_after[0] = (_queued[0], time.time() + 0.5)


def _run_deferred_preview():
    pending = _preview_after[0]
    if pending is None:
        return
    need, give_up = pending
    sent = _sent[0] >= need and time.time() - _sent_at[0] >= 0.04
    if not sent and time.time() < give_up:
        return
    _preview_after[0] = None
    _preview()
    _push_state()          # preview pushes too, but not when it failed


def _declare_actions():
    hk = _hotkeys()
    # The plane is a mirror's direction: the shared Cycle Direction steps it, and Cycle
    # Operation its join / new body — see hotkeys.py's shared actions.
    hk.provide('direction', 'Mirror', {'command': 'mirror'}, _cycle_plane)
    hk.provide('operation', 'Mirror', {'command': 'mirror'}, _cycle_operation)
    hk.provide('direction_back', 'Mirror', {'command': 'mirror'}, lambda: _cycle_plane(-1))
    hk.provide('operation_back', 'Mirror', {'command': 'mirror'}, _cycle_operation)  # a pair
    # The command itself, as a native claimant — see Extrude's.
    hk.declare(CMD_ID, CMD_NAME, CMD_DESC, owner='Mirror',
               when={'command': 'none'}, native=True)
    hk.attach()


VALUE_URL = 'http://127.0.0.1:8734'
LOG = '/tmp/fusion_mirror.log'
_TICK_SECS = 0.06
_EVENT_ID = 'SmartMirrorTick'

_created = None
_custom_evt = None
_sync = None
_poller_stop = None

_cmd = None
_sel_input = None
_running = False

_DEFAULT_SPEC = {
    'plane': 'x',            # 'x' | 'y' | 'z' | 'picked'
    'operation': 'join',     # 'join' | 'newbody' — join is isCombine, bodies only
    'compute': 'optimized',  # advanced: 'optimized' | 'identical' | 'adjust'
    # 'bodies' | 'group' | 'component' — what is mirrored when it is bodies: the ones
    # clicked, every body in the Browser groups `groups`, or every body of the component.
    # The last two keep following once made (_FOLLOW_ATTR).
    'scope': 'bodies',
    'groups': [],            # group ids; only ever replaced
}
_spec = dict(_DEFAULT_SPEC)

# What is being mirrored: native bodies or native features, never a mix (Fusion's own
# mirror takes one object type at a time). See _adopt_picks.
_picked = []
_picked_id = ()
_kind = ''                   # 'bodies' | 'features' | ''

# The plane you picked, native, or None; and the token of the last plane CLICK, whatever
# it resolved to. The click stays in the selection, so the token is what stops it being
# taken again on every later change (snapping the plane back after a cycle), and stops a
# picked face ALSO being taken as a body to mirror.
_plane_ent = None
_plane_token = ''
_sel_id = ()                 # the whole viewport selection last time we looked

_cmd_handlers = []
_cancelling = False
_previewed = False
_filter = ''                 # the selection filter set last — see _arm_filter
# What the last build actually did: 'join' can only join a copy that TOUCHES the
# original, so a mirror that lands clear of it is built as a new body instead (see
# _build) and the panel shows that in the accent, like Extrude's resolved Auto.
_resolved_op = ''
# How many things the last build mirrored — with a group or component scope, the bodies
# it names, which the panel says rather than what was clicked.
_n_inputs = 0


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _tb():
    return traceback.format_exc().replace('\n', ' | ')


# ── Talking to the window's server ───────────────────────────────────────────
# Extrude's arrangement exactly: the poller thread does ALL of the I/O.

def _post(path, payload):
    try:
        req = urllib.request.Request(
            VALUE_URL + path, data=json.dumps(payload).encode(),
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


_inbox = []
_outbox = []
_box_lock = threading.Lock()


def _send(path, payload):
    with _box_lock:
        _outbox.append((path, payload))
        _queued[0] += 1


# Extrude's: the newest panel `rev` acted on, echoed on every push (index.js, "What we
# have asked for"); and the posts queued / sent, so a cycle key's preview can wait for
# its push to go out first (_defer_preview).
_rev = [0]
_queued = [0]
_sent = [0]
_sent_at = [0.0]
_preview_after = [None]


# ── What is being mirrored, and across what ──────────────────────────────────

def _native(entity):
    try:
        return entity.nativeObject or entity
    except Exception:
        return entity


def _token(entity):
    try:
        return entity.entityToken
    except Exception:
        return repr(entity)


def _component_of(entity):
    """The component a native body or feature lives in."""
    for attr in ('parentComponent',):
        try:
            got = getattr(entity, attr, None)
            if got is not None:
                return got
        except Exception:
            pass
    return None


def _plane_component(entity):
    """The component a native construction plane or face belongs to."""
    cp = adsk.fusion.ConstructionPlane.cast(entity)
    if cp is not None:
        try:
            return cp.component
        except Exception:
            return None
    face = adsk.fusion.BRepFace.cast(entity)
    if face is not None:
        try:
            return face.body.parentComponent
        except Exception:
            return None
    return None


def _is_planar_face(entity):
    face = adsk.fusion.BRepFace.cast(entity)
    try:
        return face is not None and adsk.core.Plane.cast(face.geometry) is not None
    except Exception:
        return False


def _origin_planes(comp):
    """{'x': YZ, 'y': XZ, 'z': XY} — mirroring ACROSS an axis means across the plane
    that axis is normal to."""
    return {'x': comp.yZConstructionPlane,
            'y': comp.xZConstructionPlane,
            'z': comp.xYConstructionPlane}


def _component():
    """The component the mirror belongs in: the one what is being mirrored is in — with
    nothing clicked and "Bodies are: Component", the active one."""
    if _picked:
        return _component_of(_picked[0])
    if _scope() == 'component':
        try:
            return adsk.fusion.Design.cast(app.activeProduct).activeComponent
        except Exception:
            return None
    return None


def _scope():
    """'bodies' | 'group' | 'component'. A mirror of features is always the ones picked."""
    return 'bodies' if _kind == 'features' else _spec.get('scope', 'bodies')


def _kind_now():
    """'bodies' | 'features' | '' — what is mirrored, a group or component being bodies."""
    return 'bodies' if _scope() != 'bodies' else _kind


def _live_rec(comp):
    """The live-set record (Groups.py) the scope makes for `comp`."""
    if _scope() == 'component':
        return {'g': [], 'c': comp.id}
    return {'g': list(_spec.get('groups') or []), 'c': ''}


def _inputs(comp):
    """What is mirrored: the bodies or features clicked, or the bodies the scope names."""
    if _scope() == 'bodies':
        return list(_picked)
    design = adsk.fusion.Design.cast(app.activeProduct)
    if comp is None or design is None:
        return []
    return groups.live_bodies(design, _live_rec(comp), comp)


def _groups_of(bodies):
    """The ids of the groups these bodies are filed in, each once."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    out = []
    for b in bodies:
        g = groups.group_of(design, b) if adsk.fusion.BRepBody.cast(b) is not None else None
        if g and g not in out:
            out.append(g)
    return out


def _group_chips(comp):
    """[[id, name]] for the panel's chips: every group one of the component's bodies is
    filed in, then any chosen group that isn't one of those."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None or comp is None or _scope() != 'group':
        return []
    folders, leaves = groups._doc_groups(design)
    ids = []
    for k, _ in groups.keyed_bodies(comp):
        g = groups.top(folders, leaves.get(k))
        if g in folders and g not in ids:
            ids.append(g)
    ids += [g for g in _spec.get('groups') or [] if g not in ids]
    return [[g, groups.group_name(design, g)] for g in ids]


def _plane_label(entity):
    if adsk.fusion.ConstructionPlane.cast(entity) is not None:
        try:
            return entity.name or 'Plane'
        except Exception:
            return 'Plane'
    return 'Face'


def _armed():
    """Is the panel waiting for a plane to be clicked?"""
    return _spec.get('plane') == 'picked' and _plane_ent is None


def _arm_filter():
    """What a click in the viewport picks. Bodies (and timeline features) ordinarily, so
    a click on a face takes the body — Extrude found a SolidBodies filter does exactly
    that. While armed, planar faces instead, so the click takes the face. Construction
    planes always: they can only ever be the plane."""
    global _filter
    want = 'plane' if _armed() else 'objects'
    if _sel_input is None or want == _filter:
        return
    _filter = want
    try:
        _sel_input.clearSelectionFilter()
        names = (('PlanarFaces', 'ConstructionPlanes') if want == 'plane'
                 else ('Bodies', 'Features', 'ConstructionPlanes'))
        for f in names:
            try:
                _sel_input.addSelectionFilter(f)
            except Exception:
                pass
    except Exception:
        _log('filter failed:', _tb())


def _read_selection():
    """(objects, kind, plane) from the viewport selection, as native entities. `plane`
    is the newest plane candidate in it, or None."""
    bodies, features, plane = [], [], None
    seen = set()
    try:
        sel = ui.activeSelections
        ents = [sel.item(i).entity for i in range(sel.count)]
    except Exception:
        ents = []
    for ent in ents:
        if adsk.fusion.ConstructionPlane.cast(ent) is not None:
            plane = ent
            continue
        tok = _token(ent)
        if _plane_token and tok == _plane_token:
            continue                  # the face that IS the plane — see _plane_token
        if adsk.fusion.BRepFace.cast(ent) is not None:
            if _armed() and _is_planar_face(ent):
                plane = ent
                continue
            try:
                ent = ent.body        # a face click mirrors its body
            except Exception:
                continue
        native = _native(ent)
        key = _token(native)
        if key in seen:
            continue
        if adsk.fusion.BRepBody.cast(native) is not None:
            seen.add(key)
            bodies.append(native)
        elif adsk.fusion.Feature.cast(native) is not None:
            seen.add(key)
            features.append(native)
    # One object type at a time, like Fusion's own mirror: bodies win a mixed pick.
    if bodies:
        objs, kind = bodies, 'bodies'
    elif features:
        objs, kind = features, 'features'
    else:
        objs, kind = [], ''
    # All in ONE component — the first one's — since that is where the feature goes.
    if objs:
        comp = _component_of(objs[0])
        objs = [o for o in objs if _component_of(o) == comp]
    return objs, kind, plane


def _take_plane(entity):
    """A plane was clicked. Take it — or, when it is this component's own origin plane,
    take the axis it stands for instead."""
    global _plane_ent, _plane_token
    _plane_token = _token(entity)
    native = _native(entity)
    comp = _component()
    if comp is not None:
        owner = _plane_component(native)
        if owner is not None and owner != comp:
            _send('/mirror/error', {'error': 'That plane is in %s — pick one in %s.'
                                    % (owner.name, comp.name)})
            return False
        for axis, cp in _origin_planes(comp).items():
            if cp == native:
                _spec['plane'] = axis
                _plane_ent = None
                return True
    _plane_ent = native
    _spec['plane'] = 'picked'
    return True


def _adopt_picks():
    """Notice a change in the viewport selection. Main thread, on the tick. As in
    Extrude, only a NON-EMPTY set of objects replaces what is being mirrored — Fusion
    empties the selection as the command starts."""
    global _picked, _picked_id, _kind, _sel_id
    try:
        sel = ui.activeSelections
        now = tuple(_token(sel.item(i).entity) for i in range(sel.count))
    except Exception:
        now = ()
    if now == _sel_id:
        return False
    _sel_id = now
    objs, kind, plane = _read_selection()
    changed = False
    # Objects first: the plane is judged against the component they are in.
    ident = tuple(_token(o) for o in objs)
    if objs and ident != _picked_id:
        _picked, _picked_id, _kind = objs, ident, kind
        changed = True
        if _scope() == 'group':
            _seed_groups()
    if plane is not None and _token(plane) != _plane_token:
        changed = _take_plane(plane) or changed
        _arm_filter()
    return changed


def _seed_groups():
    """"Bodies are: Group": the groups are those of the bodies clicked."""
    gids = _groups_of(_picked)
    if gids:
        _spec['groups'] = gids
    elif _picked:
        _send('/mirror/error', {'error': '%s is not in a group.' % _picked[0].name})


def _plane_entity(comp):
    plane = _spec.get('plane', 'x')
    if plane == 'picked':
        return _plane_ent
    return _origin_planes(comp).get(plane)


# ── Building the feature ─────────────────────────────────────────────────────

_COMPUTE = {
    'optimized': adsk.fusion.PatternComputeOptions.OptimizedPatternCompute,
    'identical': adsk.fusion.PatternComputeOptions.IdenticalPatternCompute,
    'adjust': adsk.fusion.PatternComputeOptions.AdjustPatternCompute,
}


def _build(design):
    """Make the MirrorFeature the panel describes, or None. Called from executePreview
    and from execute, identically — what you saw is what you get."""
    global _resolved_op, _n_inputs
    comp = _component()
    inputs = _inputs(comp)
    _n_inputs = len(inputs)
    if not inputs:
        return None
    plane = _plane_entity(comp)
    if plane is None:
        return None
    feats = comp.features.mirrorFeatures
    bodies = _kind_now() == 'bodies'

    def attempt(combine):
        coll = adsk.core.ObjectCollection.create()
        for e in inputs:
            coll.add(e)
        inp = feats.createInput(coll, plane)
        if bodies:
            inp.isCombine = combine
        try:
            inp.patternComputeOption = _COMPUTE.get(
                _spec.get('compute'), _COMPUTE['optimized'])
        except Exception:
            pass
        made = feats.add(inp)
        _stamp_follow(design, made, comp)
        return made

    join = bodies and _spec.get('operation', 'join') == 'join'
    if join:
        try:
            made = attempt(True)
            _resolved_op = 'join'
            return made
        except Exception:
            # "No pattern instances could be intersected with the original body": the
            # copy is clear of the original, so there is nothing to join it to.
            _log('join failed, building a new body:', _tb())
    try:
        made = attempt(False)
    except Exception:
        _log('build failed:', [type(e).__name__ for e in inputs], _tb())
        raise
    _resolved_op = 'newbody' if bodies else ''
    return made


# ── Following a group or component ───────────────────────────────────────────
# A mirror made with "Bodies are: Group" or "Component" names what it follows on itself
# — Groups.py's live-set record {g, c, fp, off} — and is kept in step exactly as an
# Extrude following a group is (Extrude.py's "Following a group"): after each finished
# command and each filing in the Browser, a changed fingerprint rolls to just before
# the mirror and sets its inputEntities to the bodies there; never moved (a body only
# there after it is left alone), suppressed while there is
# nothing to mirror. The Browser add-in reads the record to warn before a group it
# follows is exploded (_group_uses).

_FOLLOW_ATTR = ('josh.mirror', 'follow')


def _stamp_follow(design, feature, comp):
    """Name what the mirror follows on it. Best-effort."""
    if _scope() == 'bodies':
        return
    try:
        rec = _live_rec(comp)
        rec['fp'] = groups.live_fingerprint(design, rec)
        rec['off'] = False
        feature.attributes.add(_FOLLOW_ATTR[0], _FOLLOW_ATTR[1], json.dumps(rec))
    except Exception:
        _log('follow stamp failed:', _tb())


def _sync_follow(design):
    """Every mirror following a group or component whose fingerprint moved, brought
    into step. Main thread, idle Fusion, marker at the end (Groups.Watcher)."""
    try:
        attrs = list(design.findAttributes(*_FOLLOW_ATTR))
    except Exception:
        return
    for a in attrs:
        try:
            f = adsk.fusion.MirrorFeature.cast(a.parent)
            if f is None or not f.isValid:
                continue
            rec = json.loads(a.value)
        except Exception:
            continue
        groups.unpin(f, tickguard.one_step('MirrorUnpin'))
        if groups.in_step(design, f, rec):             # unchanged, or changed only after it
            continue
        if groups.live_gone(design, rec):
            _log('follow: what', f.name, 'follows is gone — left as it is')
            continue
        if f.timelineObject.isSuppressed and not rec.get('off'):
            continue                                   # suppressed by hand: leave it

        def apply(feat, parts):
            coll = adsk.core.ObjectCollection.create()
            for b in parts:
                coll.add(b)
            feat.inputEntities = coll

        try:
            with tickguard.hold('mirror/follow'), tickguard.one_step('MirrorFollow'):
                n = groups.live_sync(design, f, rec, apply, _log)
                f.attributes.add(_FOLLOW_ATTR[0], _FOLLOW_ATTR[1], json.dumps(rec))
            health = ''
            try:
                if f.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
                    health = ' — ' + (f.errorOrWarningMessage or 'unhealthy')
            except Exception:
                pass
            _log('follow:', f.name, '->', n, 'bodies' if n else 'bodies (suppressed)',
                 health)
        except Exception:
            _log('follow sync failed:', _tb())


def _follow_busy():
    return bool(_running) or tickguard.busy() or tickguard._in_sketch()


# ── Getting the native palette off the screen ────────────────────────────────
# Extrude.py's "Getting the native palette off the screen", copied rather than shared for
# the reason its _Qt gives. Fusion names the palette after the command, which is what
# makes it findable by name.

_qt = None


class _Qt:
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

    def hide(self, w):
        self._setVisible(w, False)


def _qt_bind():
    global _qt
    if _qt is None:
        try:
            _qt = _Qt()
        except Exception:
            _log('no Qt bind:', _tb())
            _qt = False
    return _qt or None


_PALETTE_MARK = 'QTCommandDialogContentPanel' + CMD_ID


def _hide_palette():
    """GUI thread only — see Extrude's. Best effort."""
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
        _log('hide failed:', _tb())
    if not hidden:
        _log('palette not found — looked for', _PALETTE_MARK)


# ── The panel ────────────────────────────────────────────────────────────────

def _state_spec():
    """Everything the panel shows. The whole of it goes both ways — see update_mirror."""
    comp = _component()
    n = len(_picked) if _scope() == 'bodies' else _n_inputs
    return {
        'plane': _spec.get('plane', 'x'),
        'operation': _spec.get('operation', 'join'),
        'operationResolved': _resolved_op,
        'compute': _spec.get('compute', 'optimized'),
        'scope': _spec.get('scope', 'bodies'),
        'groups': list(_spec.get('groups') or []),
        'groupChips': _group_chips(comp),
        'hasSelection': n > 0,
        'selectionCount': n,
        # What was clicked, which the scope row is shown for (never for features).
        'pickedKind': _kind,
        'kind': _kind_now(),
        'component': comp.name if comp is not None else '',
        'pickedLabel': _plane_label(_plane_ent) if _plane_ent is not None else '',
        # Every chord that means something right now — the panel has no field, so it
        # hands all of them back. See hotkeys.py::panel_keys.
        'keys': _hotkeys().panel_keys(),
        # …and of those, the cycles, which the panel steps itself (hotkeys.cycle_keys).
        'cycleKeys': _hotkeys().cycle_keys(),
        'rev': _rev[0],
    }


def _open_panel():
    _send('/mirror/open', {'spec': _state_spec()})


def _push_state():
    _send('/mirror/update', {'spec': _state_spec()})


def _clear_selection():
    """The panel's Clear: what is being mirrored goes, the plane choice stays. Redraw
    FIRST, while every reference the running command holds is still valid — see
    Extrude's _clear_selection."""
    global _picked, _picked_id, _kind
    _picked, _picked_id, _kind = [], (), ''
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
    _push_state()


def _apply_panel_commands():
    global _plane_ent, _plane_token
    with _box_lock:
        commands = _inbox[:]
        del _inbox[:]
    if not commands:
        return
    changed = False
    for c in commands:
        action = c.get('action')
        if isinstance(c.get('rev'), (int, float)):
            _rev[0] = max(_rev[0], int(c['rev']))
        if action == 'setmirror':
            scope_was = _spec.get('scope')
            for key in _DEFAULT_SPEC:
                if key in c:
                    _spec[key] = c[key]
            if _spec.get('scope') != scope_was and _spec.get('scope') == 'group':
                _seed_groups()
            if c.get('repick'):
                _plane_ent, _plane_token = None, ''
            changed = True
        elif action == 'clearmirror':
            _clear_selection()
        elif action == 'commitmirror':
            _commit()
            return
        elif action == 'cancelmirror':
            _cancel()
            return
    if changed:
        _arm_filter()
        _preview()


def _preview():
    if _cmd is None:
        return
    try:
        _cmd.doExecutePreview()
        try:
            app.activeViewport.refresh()
        except Exception:
            pass
        _push_state()
    except Exception:
        _log('preview failed:', _tb())


def _commit():
    """Extrude's _commit: preview once more, and only commit what built."""
    if _cmd is None:
        return
    try:
        _cmd.doExecutePreview()
    except Exception:
        _log('commit preview failed:', _tb())
    if not _previewed:
        _send('/mirror/error', {'error': 'That mirror will not build.'})
        return
    try:
        _cmd.doExecute(True)
    except Exception:
        _log('commit failed:', _tb())


def _cancel():
    """doExecute(terminate=True) with the builder off — the only cancel that reaches a
    command whose palette is hidden. See Extrude's _cancel."""
    global _cancelling
    if _cmd is None:
        return
    _cancelling = True
    try:
        _cmd.doExecute(True)
    except Exception:
        _log('cancel failed:', _tb())
    finally:
        _cancelling = False


# ── The command ──────────────────────────────────────────────────────────────

class _ExecutePreview(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _previewed
        _previewed = False
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None or _build(design) is None:
                return
            # False: shown, then rolled back — see Extrude's _ExecutePreview for the
            # three things True breaks.
            args.isValidResult = False
            _previewed = True
        except Exception:
            _log('preview build failed:', _tb())


class _Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        if _cancelling:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is not None:
                _build(design)
        except Exception:
            _log('execute failed:', _tb())


class _InputChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if _adopt_picks():
                _preview()
        except Exception:
            _log('inputChanged failed:', _tb())


class _Activate(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _running
        try:
            _running = True
            _hide_palette()
            if _activated[0]:
                # Back from a view op (orbit, zoom, pan) Fusion ran nested inside this
                # command — see Extrude's _Activate. The panel is already up.
                return
            _activated[0] = True
            if _picked:
                # Started with something selected: no panel, just the mirror, with the
                # defaults — committed on the first tick (doExecute is not something to
                # call from inside activate). See _auto_commit.
                globals()['_autocommit'] = True
                return
            _open_panel()
        except Exception:
            _log('activate failed:', _tb())


class _Destroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _cmd, _sel_input, _running, _picked, _picked_id, _kind
        global _plane_ent, _plane_token, _sel_id, _filter
        _running = False
        _cmd = None
        _sel_input = None
        _picked, _picked_id, _kind = [], (), ''
        _plane_ent, _plane_token, _sel_id, _filter = None, '', (), ''
        _send('/cmd', {'action': 'cancel'})


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _cmd, _sel_input, _spec, _previewed, _picked, _picked_id, _kind
        global _plane_ent, _plane_token, _sel_id, _filter
        try:
            if self not in handlers:
                return
            cmd = args.command
            _cmd = cmd
            _spec = dict(_DEFAULT_SPEC)
            _previewed = False
            globals()['_autocommit'] = False
            globals()['_resolved_op'] = ''
            globals()['_n_inputs'] = 0
            _opened[0] = False
            _activated[0] = False
            _rev[0] = 0
            _preview_after[0] = None
            with _box_lock:
                del _inbox[:]          # a cancel the watch queued for the last command
            _plane_ent, _plane_token, _filter = None, '', ''

            cmd.isAutoExecute = False
            cmd.isExecutedWhenPreEmpted = False
            try:
                cmd.isOKButtonVisible = False
            except Exception:
                pass
            try:
                cmd.setDialogSize(1, 1)
                cmd.setDialogMinimumSize(1, 1)
            except Exception:
                pass

            sel = cmd.commandInputs.addSelectionInput(
                'mirrorSelection', 'Objects', 'Bodies or features to mirror')
            sel.setSelectionLimits(0, 0)
            _sel_input = sel
            _arm_filter()

            # The pre-selection, read NOW — Fusion clears it as the command takes over.
            _picked, _kind, plane = _read_selection()
            _picked_id = tuple(_token(o) for o in _picked)
            _sel_id = ()
            if plane is not None:
                _take_plane(plane)
                _arm_filter()

            del _cmd_handlers[:]
            for evt, handler in ((cmd.activate, _Activate()),
                                 (cmd.executePreview, _ExecutePreview()),
                                 (cmd.execute, _Execute()),
                                 (cmd.inputChanged, _InputChanged()),
                                 (cmd.destroy, _Destroy())):
                evt.add(handler)
                _cmd_handlers.append(handler)
        except Exception:
            _log('created failed:', _tb())


# ── The tick ─────────────────────────────────────────────────────────────────

# Set by _Activate when the command started on a pre-selection: mirror it straight away
# with the defaults (X of its component's origin, Join) and never show the panel. The
# panel is only for starting with nothing selected — or for when that fails.
_autocommit = False


def _auto_commit():
    global _autocommit
    _autocommit = False
    try:
        _cmd.doExecutePreview()
    except Exception:
        _log('auto preview failed:', _tb())
    if _previewed:
        try:
            _cmd.doExecute(True)
            return
        except Exception:
            _log('auto commit failed:', _tb())
    # It would not build: open the panel on it, saying so, rather than end in silence.
    _open_panel()
    _send('/mirror/error', {'error': 'That mirror will not build with the defaults.'})


class _Tick(adsk.core.CustomEventHandler):
    def notify(self, args):
        if not _running:
            return
        try:
            if _autocommit:
                _auto_commit()
                return
            if _adopt_picks():
                _preview()
            _apply_panel_commands()
            _run_deferred_preview()
        except Exception:
            _log('tick failed:', _tb())


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
# Set once this command's open has actually been posted (_poller). Until then there is
# no panel to have lost: a slow start (a big selection, the auto-commit's preview)
# counted through otherwise queued a cancel that shut the panel the moment it appeared.
_opened = [False]
# Set by this command's first activate; a later one is Fusion coming back from a view op.
_activated = [False]


def _watch_panel(got, action):
    """Extrude's _watch_panel: end the hidden command once the window has stopped
    showing its panel for a beat, so a replaced panel can't leave Fusion busy."""
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
    """All of the network I/O, and the only thing that fires the tick — Extrude's."""
    while not stop.is_set():
        try:
            with _box_lock:
                pending = _outbox[:]
                del _outbox[:]
            for path, payload in pending:
                if _post(path, payload) and path == '/mirror/open':
                    _opened[0] = True
                _sent[0] += 1
                _sent_at[0] = time.time()

            if _running:
                got = _get('/mirror', timeout=1)
                commands = (got or {}).get('commands') or []
                if commands:
                    with _box_lock:
                        _inbox.extend(commands)
                app.fireCustomEvent(_EVENT_ID, '')
                if got is not None and not got.get('windowAlive', True):
                    _heal_window()
                _watch_panel(got, 'cancelmirror')
            else:
                _lost[0] = 0.0
        except Exception:
            pass
        stop.wait(_TICK_SECS)


# ── Lifecycle ────────────────────────────────────────────────────────────────

def run(context):
    global _created, _custom_evt, _sync, _poller_stop
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
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

        _poller_stop = threading.Event()
        threading.Thread(target=_poller, args=(_poller_stop,), daemon=True).start()

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        try:
            _declare_actions()
        except Exception:
            _log('could not declare actions:', _tb())

        global _follow_watch
        _follow_watch = groups.Watcher('MirrorFollowSync', _sync_follow, _log, _follow_busy)
        _follow_watch.start()
    except Exception:
        ui.messageBox('Mirror run() failed:\n%s' % traceback.format_exc())


_follow_watch = None


def stop(context):
    global _created, _custom_evt, _sync, _poller_stop, _cmd, _sel_input, _running
    global _follow_watch
    try:
        if _follow_watch is not None:
            _follow_watch.stop()
    except Exception:
        pass
    _follow_watch = None
    try:
        if _running and _cmd is not None:
            _cancel()
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

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        try:
            _hotkeys().withdraw('Mirror')
        except Exception:
            pass
        handlers.clear()
        del _cmd_handlers[:]
    except Exception:
        pass
