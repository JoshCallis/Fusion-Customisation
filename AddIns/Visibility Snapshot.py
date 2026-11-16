"""
fusion_visibility_snapshot.py
─────────────────────────────
Shared visibility-management utilities for Fusion 360 add-ins.

USAGE
─────
    import os, sys
    _ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ADDINS_DIR not in sys.path:
        sys.path.insert(0, _ADDINS_DIR)
    import fusion_visibility_snapshot as fvs

PUBLIC API
──────────
    fvs.take_snapshot(design, occ)
        Record the current child-occurrence visibility of occ (or root if None).
        Writes to disk, keyed by document name and occurrence path.

    fvs.restore_snapshot(design, occ)
        Apply the saved child-occurrence visibility of occ (or root if None).
        Occurrences not in the saved record are made visible.

    fvs.isolate_occurrence(design, target_occ)
        Hide every occurrence not on the root→target path.
        Only touches the minimum number of nodes needed — does not recurse
        into already-hidden subtrees.

    fvs.find_occurrence(design, comp)
        Return the first occurrence whose .component == comp, or None.

    fvs.find_parent_occurrence(design, comp)
        Return the parent occurrence of comp's occurrence, or None if comp
        sits at root level.

    fvs.is_suspended() / fvs.view_occ(design) / fvs.peek(design, occ) /
    fvs.set_suspended(design, on)
        The Browser's suspend mode: navigation PEEKS (visibility only) while the
        active component, sketch and command stay put. See § Suspend mode: peek.
"""

import json
import os
import re as _re
import sys
import datetime
import time as _time
import traceback as _traceback

# Only ever loaded inside Fusion's Python runtime, so adsk is always importable.
try:
    import adsk.core, adsk.fusion
except Exception:
    adsk = None

_SNAPSHOT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'Visibility Snapshots.json',
)

_SHARED_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'Shared State.json',
)

_LOG_FILE = '/tmp/fusion_visibility_snapshot.log'

# ── Keeping the pollers out ──────────────────────────────────────────────────
# Every function below that CHANGES the model carries @_holds_model. Flipping a
# bulb or moving the timeline marker rebuilds the scene graph, and Fusion pumps
# its task queue while it does — which is what delivers the Browser's 70 ms tick
# and the Dimension Value tick. Without the latch those ticks walk the tree
# mid-rebuild; the .hang spindumps show what that costs. See Tick Guard.py.
#
# Fails open: no latch is worse than no visibility engine, so a missing or broken
# Tick Guard.py leaves the behaviour exactly as it was before this existed.
class _NullGuard:
    def __enter__(self): return True
    def __exit__(self, *exc): return False


try:
    import types as _types
    _TG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Tick Guard.py')
    tickguard = _types.ModuleType('tickguard')
    tickguard.__file__ = _TG_PATH
    with open(_TG_PATH, 'r', encoding='utf-8') as _f:
        exec(compile(_f.read(), _TG_PATH, 'exec'), tickguard.__dict__)
except Exception:
    tickguard = None


def _hold(name):
    return _NullGuard() if tickguard is None else tickguard.hold(name)


def one_step(name):
    """Every model write inside is ONE undo step — Tick Guard.py § One undo step.
    Callers wrap a whole user action (a jump, a toggle) so it lands as one step;
    the functions below group themselves too, for callers that don't."""
    if tickguard is None or not hasattr(tickguard, 'one_step'):
        return _NullGuard()
    return tickguard.one_step(name)


def _holds_model(fn):
    """Mark a function as one that changes the model: pollers stand down for it,
    and its writes are one undo step (or part of the caller's, when it has one)."""
    def wrapped(*a, **kw):
        with _hold('visibility/' + fn.__name__), one_step('Visibility'):
            return fn(*a, **kw)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    wrapped.__wrapped__ = fn
    return wrapped


def _caller():
    """The add-in frame that asked for this, for the log. Skips _holds_model's own
    wrapper, which would otherwise be all any decorated function ever reported."""
    stack = [f for f in _traceback.extract_stack()[:-1]   # drop _caller itself
             if f.name != 'wrapped']                      # …and every latch wrapper
    return stack[-2] if len(stack) >= 2 else (stack[-1] if stack else None)


def _log(*parts):
    try:
        ts  = datetime.datetime.now().strftime('%H:%M:%S.%f')
        msg = '  '.join(str(p) for p in parts)
        with open(_LOG_FILE, 'a', encoding='utf-8') as _f:
            _f.write(f'[{ts}] {msg}\n')
    except Exception:
        pass


# ── Bulb batch ────────────────────────────────────────────────────────────────
# A Browser jump un-isolates (ledger values back: siblings ON) and then re-isolates
# onto the target (siblings OFF again), so a sibling jump wrote ~2× every sibling's
# bulb where two actually change. Inside `with bulb_batch():` the occurrence bulbs and
# snapshot folders below are written to a buffer and read back from it, so every step
# — ledger notes included — sees exactly the state it saw before; on the way out only
# the net differences reach Fusion, compared first. Same end state, no intermediate
# ones. Only the helpers below go through it: anything else reads Fusion directly.
_vbuf = None       # {key: [entity, attr, value]} while a bulb_batch is open


def _vget(ent, attr, key):
    if _vbuf is not None and key in _vbuf:
        return _vbuf[key][2]
    return getattr(ent, attr)


def _vset(ent, attr, key, value):
    if _vbuf is None:
        setattr(ent, attr, value)
    else:
        _vbuf[key] = [ent, attr, value]


def _obulb(occ):
    if _vbuf is None:
        return occ.isLightBulbOn
    return _vget(occ, 'isLightBulbOn', ('o', occ.fullPathName))


def _set_obulb(occ, value):
    if _vbuf is None:
        occ.isLightBulbOn = value
    else:
        _vset(occ, 'isLightBulbOn', ('o', occ.fullPathName), bool(value))


class _BulbBatch:
    __slots__ = ('own',)

    def __enter__(self):
        global _vbuf
        self.own = _vbuf is None
        if self.own:
            _vbuf = {}
        return True

    def __exit__(self, *exc):
        global _vbuf
        if self.own:
            buf, _vbuf = _vbuf, None
            for ent, attr, value in buf.values():
                try:
                    if bool(getattr(ent, attr)) != bool(value):
                        setattr(ent, attr, value)
                except Exception:
                    pass
        return False


def bulb_batch():
    """Buffer the isolation bulb/folder writes inside; write only the net change."""
    return _BulbBatch()


def _vis_summary(occs):
    """Return a compact {name: bool} dict for a flat occurrence collection."""
    try:
        return {occ.name: _obulb(occ) for occ in occs}
    except Exception:
        return {}

# Folders OWNED BY THE SNAPSHOT SYSTEM.
# Body / sketch / origin folders are NOT here any more — they are owned
# exclusively by the visibility-policy layer below (apply_policy), which is the
# single authority for them.  The snapshot only records/restores the
# construction folder so that isolate's ancestor-hiding stays identical to
# before; everything else is recomputed from the global toggles each time the
# active component changes.
_FOLDER_PROPS = (
    'isConstructionFolderLightBulbOn',
)

def _read_folders(comp):
    state = {}
    for prop in _FOLDER_PROPS:
        try:
            state[prop] = _vget(comp, prop, ('f', comp.id, prop))
        except Exception:
            pass
    return state

def _write_folders(comp, state, default=True):
    for prop in _FOLDER_PROPS:
        val = state.get(prop, default) if state else default
        try:
            _vset(comp, prop, ('f', comp.id, prop), val)
        except Exception:
            pass

def _hide_folders(comp):
    _write_folders(comp, {}, default=False)


# ── File I/O ──────────────────────────────────────────────────────────────────

def _load():
    try:
        with open(_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_peek_cache = {'key': None, 'data': {}}


def _peek(key, default=None):
    """One top-level string value of the file, from a parse kept until the file changes
    (its stat). For readers on every tick: recall_active_occ runs on each Browser tick,
    a dozen a second, and parsing the whole ~90 KB file each time was most of its cost.
    Only for plain values — never hand out the cached dicts (callers mutate _load's)."""
    try:
        st = os.stat(_SNAPSHOT_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default
    c = _peek_cache
    if c['key'] != stamp:
        c['data'] = _load()
        c['key'] = stamp
    v = c['data'].get(key, default)
    return v if isinstance(v, (str, int, float, bool, type(None))) else default


# Fusion names each save as a new document version, and every version that gets a
# snapshot stays in this file forever unless something drops it. Left alone it grows
# without bound (one design had reached 96 versions), and the whole file is parsed on
# each _load(). Keep only the newest few versions of any one document.
_KEEP_VERSIONS = 5
_VERSION_KEY = _re.compile(r'^(.*?)\s*(?:v(\d+)|\(v(\d+)[^)]*\))\s*$')


def _prune(data):
    """Drop all but the newest _KEEP_VERSIONS snapshots per document.

    Keys that aren't '<document> v<n>' — the '__…__' internals and unversioned
    names — are never touched.
    """
    groups = {}
    keep = set()
    for k in data:
        m = None if k.startswith('__') else _VERSION_KEY.match(k)
        if m:
            groups.setdefault(m.group(1).strip(), []).append(
                (int(m.group(2) or m.group(3)), k))
        else:
            keep.add(k)
    for items in groups.values():
        items.sort(reverse=True)
        keep.update(k for _, k in items[:_KEEP_VERSIONS])
    return {k: v for k, v in data.items() if k in keep}


_last_saved = (None, None)   # (text, file stat) of this copy's last write


def _stat_key():
    try:
        st = os.stat(_SNAPSHOT_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _save(data):
    """Compact, in one write: it runs several times per jump, and indent=2 takes
    json's pure-Python encoder (~230k tiny writes per jump, ~5× the compact cost).
    Skips a rewrite identical to this copy's last one — but only while the file is
    still that write (every add-in exec's its own copy of this module)."""
    global _last_saved
    try:
        data = _prune(data)
    except Exception:
        pass          # pruning is housekeeping — never lose a save over it
    text = json.dumps(data)
    if text == _last_saved[0] and _stat_key() == _last_saved[1]:
        return
    with open(_SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
        f.write(text)
    _last_saved = (text, _stat_key())


def _doc(design):
    try:
        return design.parentDocument.name
    except Exception:
        return '_unknown'


# The occurrence snapshots are keyed per document LINEAGE, like the leaf store. They
# were keyed on _doc() — the versioned name — so every save started an empty record:
# the next restore of a level found nothing, made every child visible, and the hides
# made before the save were gone for good (260612 v570 → v571, 2026-10-07: Global,
# both legs, Boot Door, Drawer, Disregard all back on at the next sketch entry).
# '__'-prefixed so _prune never takes it for a version.
def _snap_key(design):
    return '__snap__|' + _lineage(design)


def _saved_level(data, design, path):
    """The saved record of one level: the lineage's, else (records made before it was
    keyed so) the newest versioned record of the same document that has the level."""
    rec = (data.get(_snap_key(design)) or {}).get(path)
    if rec is not None:
        return rec
    base = _VERSION_KEY.match(_doc(design))
    base = base.group(1).strip() if base else _doc(design)
    best = None
    for k, v in data.items():
        m = None if k.startswith('__') else _VERSION_KEY.match(k)
        if m and m.group(1).strip() == base and isinstance(v, dict) and \
                isinstance(v.get(path), dict):
            n = int(m.group(2) or m.group(3))
            if best is None or n > best[0]:
                best = (n, v[path])
    return best[1] if best else None


# ── Internal: path + occurrence list from root to a target ────────────────────

def _occ_path_str(occ):
    """
    Build the slash-joined path string for an occurrence by walking up via
    assemblyContext.  More reliable than == comparison on wrapper objects.
    """
    parts = []
    current = occ
    while current is not None:
        parts.append(current.name)
        try:
            current = current.assemblyContext
        except Exception:
            break
    return '/'.join(reversed(parts))


def _find_occ_info(design, target_occ):
    """
    Return (path_str, occ_list) for root→target_occ.
    path_str is slash-joined occurrence names, e.g. 'Assembly:1/Shelf:1'.
    Returns ('', []) when target_occ is None (root context).
    """
    if target_occ is None:
        return '', []

    target_path = _occ_path_str(target_occ)
    if not target_path:
        return '', []

    def _search(occs, s_acc, o_acc):
        for occ in occs:
            s = s_acc + [occ.name]
            o = o_acc + [occ]
            joined = '/'.join(s)
            if joined == target_path:
                return joined, o
            # Only recurse if target is under this node
            if target_path.startswith(joined + '/'):
                r = _search(occ.childOccurrences, s, o)
                if r is not None:
                    return r
        return None

    result = _search(design.rootComponent.occurrences, [], [])
    return result if result is not None else ('', [])


# ── Snapshot ──────────────────────────────────────────────────────────────────

def _scan(occs, prefix='', occ_led=None):
    """
    Recursively capture visibility state — the user's INTENT, not the isolated view.
    A bulb isolation switched is recorded with the value it had before isolation
    (occ_led, the isolate ledger), so an isolated sibling is recorded as shown.
    Stops recursing into hidden occurrences — no need to record their children.
    """
    out = {}
    for occ in occs:
        p = (prefix + '/' + occ.name) if prefix else occ.name
        vis = _obulb(occ)
        if occ_led and p in occ_led:
            vis = bool(occ_led[p])
        node = {'v': vis}
        if vis:
            ch = _scan(occ.childOccurrences, p, occ_led)
            if ch:
                node['c'] = ch
        out[occ.name] = node
    return out


def take_snapshot(design, occ):
    """
    Record visibility of all sub-occurrences within occ (or root if None).
    Persists to fusion_visibility_snapshots.json keyed by document + path.

    Records INTENT: anything isolation hid is recorded as it was before isolation.
    This used to record the raw bulbs, and it is called from places that run while
    you are descended — every nav add-in's run() and documentOpened snapshot ROOT.
    Reloading the add-ins while deep in a component therefore wrote isolation's
    hiding (Legs, Legs(Mirror)…) into the root snapshot as though you had hidden
    them, and the next restore of that level (Go Up to the top, Go to Top's
    fallback, the isolate toggle) hid them for real.
    """
    try:
        children = occ.childOccurrences if occ else design.rootComponent.occurrences
        before   = _vis_summary(children)
        path, _ = _find_occ_info(design, occ)
        doc = _doc(design)
        data = _load()
        occ_led, fld_led = _ledger_read(data)
        snapshot = _scan(children, path, occ_led)
        comp = occ.component if occ else design.rootComponent
        folders = _read_folders(comp)
        try:
            folders.update(fld_led.get(comp.id) or {})
        except Exception:
            pass
        snapshot['__f__'] = folders
        data.setdefault(_snap_key(design), {})[path] = snapshot
        _save(data)
        _log('TAKE_SNAPSHOT',
             f'doc={doc!r}', f'path={path!r}',
             f'occ={occ.name if occ else "root"}',
             f'vis={before}')
        _log('  caller:', _caller())
    except Exception as _e:
        _log('TAKE_SNAPSHOT ERROR', _e, _traceback.format_exc())


def _apply(occs, saved, path='', occ_led=None):
    # Only touch direct children — never recurse.
    # Each level's visibility is managed by its own snapshot; recursing into
    # children here would overwrite sub-level state with stale parent-snapshot data.
    #
    # A child with no record keeps its bulb — or, if isolation is holding it, gets the
    # value isolation found (the ledger). It used to be switched ON: a record that went
    # missing (every save, while records were keyed per version) brought back
    # everything you had hidden at that level.
    occ_led = occ_led or {}
    for occ in occs:
        node    = saved.get(occ.name) if saved else None
        old_vis = _obulb(occ)
        cp      = (path + '/' + occ.name) if path else occ.name
        if node is not None:
            new_vis, why = bool(node['v']), ''
        elif cp in occ_led:
            new_vis, why = bool(occ_led[cp]), ' (no record → ledger)'
        else:
            continue
        if old_vis != new_vis:
            _log(f'  _apply {occ.name}: {old_vis} → {new_vis}{why}')
        _set_obulb(occ, new_vis)


@_holds_model
def restore_snapshot(design, occ):
    """
    Apply saved visibility to sub-occurrences of occ (or root if None).
    Anything not found in the saved record keeps its bulb (see _apply).
    """
    try:
        path, _ = _find_occ_info(design, occ)
        doc = _doc(design)
        data = _load()
        saved = _saved_level(data, design, path)
        children = occ.childOccurrences if occ else design.rootComponent.occurrences
        before   = _vis_summary(children)
        _log('RESTORE_SNAPSHOT',
             f'doc={doc!r}', f'path={path!r}',
             f'occ={occ.name if occ else "root"}',
             f'saved_keys={list(k for k in (saved or {}) if not k.startswith("__"))}',
             f'before={before}')
        _log('  caller:', _caller())
        if saved is None:
            _log('  no saved snapshot — bulbs kept (isolation\'s undone from the ledger)')
        _apply(children, saved, path, _ledger_read(data)[0])
        comp = occ.component if occ else design.rootComponent
        _write_folders(comp, saved.get('__f__') if saved else None, default=True)
        after = _vis_summary(children)
        _log(f'  after={after}')
        _ledger_settle(path, [c.name for c in children], comp)
    except Exception as _e:
        _log('RESTORE_SNAPSHOT ERROR', _e, _traceback.format_exc())


def _ledger_settle(path, child_names, comp):
    """A level just restored from its snapshot is back at intent, so isolation no
    longer owns those bulbs: drop their ledger entries. Without this, climbing out
    with Go Up (snapshot-driven, never touching the ledger) left the entries behind,
    and a later un-isolate or snapshot could apply them over a hide you made since."""
    try:
        data = _load()     # fresh: bulb flips above can run other add-ins' writers
        occ_led, fld_led = _ledger_read(data)
        keys = [(path + '/' + n) if path else n for n in child_names]
        cid = getattr(comp, 'id', None)
        if not any(k in occ_led for k in keys) and cid not in fld_led:
            return
        for k in keys:
            occ_led.pop(k, None)
        fld_led.pop(cid, None)
        data[_ISOLATE_LEDGER] = {'occ': occ_led, 'folders': fld_led}
        _save(data)
    except Exception as _e:
        _log('LEDGER_SETTLE ERROR', _e)


# ── Isolation ─────────────────────────────────────────────────────────────────

@_holds_model
def isolate_occurrence(design, target_occ):
    """
    Hide siblings along the entire root→target path without touching snapshots.
    Callers are responsible for calling take_snapshot before this function so that
    only the currently active level (the one with user-intended visibility) is recorded.
    """
    if target_occ is None:
        return
    _, path = _find_occ_info(design, target_occ)
    if not path:
        return

    target_name = path[0].name
    for occ in design.rootComponent.occurrences:
        _set_obulb(occ, occ.name == target_name)
    _hide_folders(design.rootComponent)
    for i in range(len(path) - 1):
        target_name = path[i + 1].name
        for child in path[i].childOccurrences:
            _set_obulb(child, child.name == target_name)
        _hide_folders(path[i].component)


# ── Occurrence search ─────────────────────────────────────────────────────────

# A component whose name contains this is a PASS-THROUGH wrapper: the browser never
# draws its row (it lifts the contents into the parent instead), and the nav commands
# step over it, so it can never become the active component. Nothing about the Fusion
# hierarchy changes — it is purely how Josh's tools present and traverse it.
DISREGARD_TAG = 'disregard'


def is_disregarded(x):
    """True for an Occurrence or Component marked as a pass-through wrapper."""
    try:
        name = x.component.name if hasattr(x, 'component') else x.name
    except Exception:
        return False
    return DISREGARD_TAG in (name or '').lower()


def find_occurrence(design, comp):
    """Return the first occurrence whose .component == comp, or None."""
    def _s(occs):
        for occ in occs:
            if occ.component == comp:
                return occ
            r = _s(occ.childOccurrences)
            if r is not None:
                return r
        return None
    return _s(design.rootComponent.occurrences)


def find_parent_occurrence(design, comp):
    """
    Return the occurrence that directly contains comp's occurrence.
    Returns None when comp's occurrence is a direct child of root.
    """
    def _s(occs, parent):
        for occ in occs:
            if occ.component == comp:
                return parent
            r = _s(occ.childOccurrences, occ)
            if r is not None:
                return r
        return None

    for occ in design.rootComponent.occurrences:
        if occ.component == comp:
            return None  # direct child of root — parent is root, not an occurrence
        r = _s(occ.childOccurrences, occ)
        if r is not None:
            return r
    return None


# ── Active occurrence tracking ────────────────────────────────────────────────

def remember_activation(occ):
    """Persist the path of the most recently activated occurrence."""
    try:
        data = _load()
        data['__active_occ__'] = _occ_path_str(occ) if occ else ''
        _save(data)
    except Exception:
        pass


def recall_active_occ(design):
    """
    The active occurrence: the stored path, VALIDATED against Fusion's own idea of what
    is active. Fusion always wins.

    `__active_occ__` is a hand-maintained mirror, written from a dozen call sites spread
    across the add-ins. Anything that activates a component without going through one of
    them — Fusion's own browser, an undo, a document reopen, or simply a code path that
    forgot — leaves it stale, pointing at a component you left long ago. That is
    expensive, because this answer decides which level gets snapshotted and which path
    gets isolated: a stale value silently hides the wrong things and the effect outlives
    the navigation that caused it.

    So the stored path is a HINT, never the truth. Its one genuine job is to say WHICH
    INSTANCE is active when several occurrences share a component, a distinction
    design.activeComponent cannot express. Whenever the hint disagrees with Fusion, or
    Fusion is sitting at the root, the hint is discarded.
    """
    try:
        if design.activeComponent == design.rootComponent:
            return None            # at the top: nothing is active, whatever we stored
    except Exception:
        pass

    result = None
    try:
        path_str = _peek('__active_occ__', '')
        if path_str:
            occs = design.rootComponent.occurrences
            for part in path_str.split('/'):
                result = None
                for occ in occs:
                    if occ.name == part:
                        result = occ
                        break
                if result is None:
                    break
                occs = result.childOccurrences
    except Exception:
        result = None

    try:
        if result is not None and result.component == design.activeComponent:
            return result          # hint agrees with Fusion — keep it, it names the instance
    except Exception:
        pass
    try:
        return find_occurrence(design, design.activeComponent)   # derive from truth
    except Exception:
        return None


# ── isolate ledger ────────────────────────────────────────────────────────────
# Isolation switches bulbs along the path you descend: siblings off, the path itself on.
# Nothing used to record that, so undoing it meant replaying a saved snapshot — and a
# snapshot cannot tell "the user hid this" apart from "isolate hid this". Replaying one
# either resurrected a deliberate hide or, worse, re-applied isolate's hiding as though
# it were intent, which is how work ended up stuck hidden.
#
# The ledger removes the guesswork by recording each bulb's value BEFORE isolate touched
# it. Un-isolating writes those values back, which is an exact undo in both directions:
# a sibling isolate hid comes back on, and something isolate forced ON over a deliberate
# hide goes back off. A bulb isolate never changed is never recorded and never touched.
#
# It covers BOTH halves of what isolation touches:
#   'occ'     - occurrence bulbs, {path: value before}
#   'folders' - each component's body/sketch group flags, {component id: {prop: before}},
#               switched off by _hide_folders. These drive BODY visibility, and leaving
#               them out is what made an earlier attempt look like it fixed nothing: the
#               bulbs came back but ~98 bodies kept flipping with every navigation.
_ISOLATE_LEDGER = '__isolate_prev__'


def _occ_from_path_str(design, path_str):
    """Resolve a slash-joined occurrence path back to a live Occurrence, or None."""
    if not path_str:
        return None
    try:
        occs = design.rootComponent.occurrences
        result = None
        for part in path_str.split('/'):
            result = None
            for occ in occs:
                if occ.name == part:
                    result = occ
                    break
            if result is None:
                return None
            occs = result.childOccurrences
        return result
    except Exception:
        return None


def _ledger_read(data):
    led = data.get(_ISOLATE_LEDGER)
    if not isinstance(led, dict):
        led = {}
    occ = led.get('occ');     occ = occ if isinstance(occ, dict) else {}
    fld = led.get('folders'); fld = fld if isinstance(fld, dict) else {}
    return occ, fld


def _ledger_note(occ_changes, folder_changes):
    """Record what isolation is about to change. FIRST write wins: isolation is applied
    in stages from root downwards, and a later stage must not overwrite the original
    pre-isolate value with an intermediate one."""
    if not occ_changes and not folder_changes:
        return
    try:
        data = _load()
        occ, fld = _ledger_read(data)
        for path_str, prev in occ_changes:
            if path_str and path_str not in occ:
                occ[path_str] = bool(prev)
        for comp_id, prev in folder_changes:
            if comp_id and comp_id not in fld:
                fld[comp_id] = dict(prev)
        data[_ISOLATE_LEDGER] = {'occ': occ, 'folders': fld}
        _save(data)
    except Exception as _e:
        _log('LEDGER_NOTE ERROR', _e)


def _hide_folders_recorded(comp, folder_changes):
    """_hide_folders, but noting the group flags it is about to switch off."""
    prev = _read_folders(comp)
    if any(bool(v) for v in prev.values()):
        try:
            folder_changes.append((comp.id, prev))
        except Exception:
            pass
    _hide_folders(comp)


@_holds_model
def unisolate_from_ledger(design):
    """Put every bulb isolation changed back to what it was, then forget the ledger.

    Returns True if a ledger existed, so the caller can skip the old snapshot replay for
    BULBS. It returns False when nothing is recorded — a session predating the ledger —
    and the caller must fall back. Either way the caller still has to restore folder
    state separately.
    """
    try:
        data = _load()
        occ_led, fld_led = _ledger_read(data)
        if not occ_led and not fld_led:
            return False
        restored = 0
        for path_str, prev in occ_led.items():
            o = _occ_from_path_str(design, path_str)
            if o is not None and bool(_obulb(o)) != bool(prev):
                _set_obulb(o, bool(prev))
                restored += 1
        if fld_led:
            by_id = {}
            try:
                for c in design.allComponents:
                    by_id[c.id] = c
            except Exception:
                pass
            for comp_id, prev in fld_led.items():
                c = by_id.get(comp_id)
                if c is not None:
                    _write_folders(c, prev)
                    restored += 1
        data[_ISOLATE_LEDGER] = {'occ': {}, 'folders': {}}
        _save(data)
        _log('UNISOLATE_FROM_LEDGER',
             f'bulbs={len(occ_led)}', f'folders={len(fld_led)}', f'restored={restored}')
        return True
    except Exception as _e:
        _log('UNISOLATE_FROM_LEDGER ERROR', _e, _traceback.format_exc())
        return False


@_holds_model
def restore_folders_only(design, occ):
    """The FOLDER half of restore_snapshot: body/sketch group state for one level, with
    the occurrence bulbs left strictly alone.

    Exists so un-isolating can take bulbs from the ledger (exact) while still taking
    group state from the snapshot (the only place it is recorded). Bypassing
    restore_snapshot wholesale is what stranded ~95 bodies on the first attempt.
    """
    try:
        path, _ = _find_occ_info(design, occ)
        saved = _saved_level(_load(), design, path)
        comp = occ.component if occ else design.rootComponent
        _write_folders(comp, saved.get('__f__') if saved else None, default=True)
    except Exception as _e:
        _log('RESTORE_FOLDERS_ONLY ERROR', _e)


@_holds_model
def isolate_occurrence_no_snapshot(design, target_occ, keep=None):
    """Hide siblings along the entire root→target path without touching snapshots.

    keep: a second occurrence whose root→keep path stays lit too — a peek (see the
    suspend section) isolates onto what you are looking at while the active component
    stays on screen. At each level a child is lit when it is on EITHER path.

    Every bulb this changes is recorded in the isolate ledger, with the value it had
    beforehand, so the change can be undone exactly.
    """
    if target_occ is None:
        return
    _, path = _find_occ_info(design, target_occ)
    if not path:
        return
    _, kpath = _find_occ_info(design, keep) if keep is not None else ('', [])
    lit = {_occ_path_str(o) for o in path} | {_occ_path_str(o) for o in kpath}
    # Every container on either path, root first, each once: its children are the
    # level isolation decides. The two leaves' own children are never touched, and
    # nor is anything inside the target: with keep below it (looking at a component
    # that holds the active one), the whole target shows, exactly as isolating onto
    # it normally does — its other children were being switched off.
    tpath = _occ_path_str(target_occ)
    levels, seen = [(None, '')], {''}
    for p in (path, kpath):
        for o in p[:-1]:
            s = _occ_path_str(o)
            if s == tpath or s.startswith(tpath + '/'):
                continue
            if s not in seen:
                seen.add(s)
                levels.append((o, s))
    changes, folder_changes = [], []
    for container, cpath in levels:
        kids = container.childOccurrences if container else design.rootComponent.occurrences
        for child in kids:
            cp = (cpath + '/' + child.name) if cpath else child.name
            want = cp in lit
            prev = bool(_obulb(child))
            if prev != want:
                changes.append((cp, prev))
            _set_obulb(child, want)
        _hide_folders_recorded(container.component if container else design.rootComponent,
                               folder_changes)
    _ledger_note(changes, folder_changes)
    _clear_widened()
    _log('ISOLATE', f'target={target_occ.name}',
         f'keep={keep.name if keep is not None else "-"}',
         f'bulbs={len(changes)}', f'folders={len(folder_changes)}')


def remember_sketch_entry_occ(occ):
    """Persist the occurrence that was active when entering sketch edit mode."""
    try:
        data = _load()
        data['__sketch_entry_occ__'] = _occ_path_str(occ) if occ else ''
        _save(data)
    except Exception:
        pass


def recall_sketch_entry_occ(design):
    """Return the occurrence stored by remember_sketch_entry_occ, or None."""
    try:
        path_str = _load().get('__sketch_entry_occ__', '')
        if not path_str:
            return None
        occs = design.rootComponent.occurrences
        result = None
        for part in path_str.split('/'):
            result = None
            for occ in occs:
                if occ.name == part:
                    result = occ
                    break
            if result is None:
                return None
            occs = result.childOccurrences
        return result
    except Exception:
        return None


def clear_sketch_entry_occ():
    """Clear the stored sketch entry occurrence."""
    try:
        data = _load()
        data.pop('__sketch_entry_occ__', None)
        _save(data)
    except Exception:
        pass


def remember_sketch_cam_delta(dx, dy, dz):
    """Save the entry camera delta (definition_pos − instance_pos) for exit correction."""
    try:
        data = _load()
        data['__sketch_cam_delta__'] = [dx, dy, dz]
        _save(data)
    except Exception:
        pass


def recall_sketch_cam_delta():
    """Return (dx, dy, dz) saved by remember_sketch_cam_delta, or None."""
    try:
        d = _load().get('__sketch_cam_delta__')
        return tuple(d) if d else None
    except Exception:
        return None


def clear_sketch_cam():
    """Erase the saved sketch camera delta."""
    try:
        data = _load()
        data.pop('__sketch_cam_delta__', None)
        _save(data)
    except Exception:
        pass


def remember_pre_sketch_marker(design):
    """Store the timeline marker position before sketch edit rolls it back.

    Call this when SketchActivate is about to run and we are NOT already in a
    sketch.  Entering sketch edit makes Fusion roll the timeline marker back to
    the sketch's position; for sketches that sit early in the timeline this
    temporarily reverts every later structural operation (e.g. the CutPaste
    that moved a sibling component inside the parent), so siblings appear to
    pop out of the component for the duration of the edit.
    """
    try:
        data = _load()
        data['__pre_sketch_marker__'] = {
            'doc': _doc(design),
            'pos': design.timeline.markerPosition,
        }
        _save(data)
        _log('REMEMBER_MARKER', f'pos={design.timeline.markerPosition}/{design.timeline.count}')
    except Exception as _e:
        _log('REMEMBER_MARKER ERROR', _e)


@_holds_model
def restore_pre_sketch_marker(design):
    """Roll the timeline marker forward to the position saved before sketch entry.

    Safe to call repeatedly (no-op once the marker is at or past the saved
    position).  Works while sketch edit is active — the sketch stays editable
    and the rest of the model returns to its true structure.  Also needed after
    SketchStop: when the marker was moved during the edit, Fusion's own
    exit-restore can land it short of the end, leaving the reverted structure
    in place permanently.
    """
    try:
        saved = _load().get('__pre_sketch_marker__')
        if not saved or saved.get('doc') != _doc(design):
            return
        tl = design.timeline
        pos = min(int(saved.get('pos', tl.count)), tl.count)
        if tl.markerPosition < pos:
            _log('RESTORE_MARKER', f'{tl.markerPosition} → {pos}/{tl.count}')
            tl.markerPosition = pos
    except Exception as _e:
        _log('RESTORE_MARKER ERROR', _e)


_shared_cache = None      # (mtime_ns, size) -> parsed dict, as (stamp, data)


def _read_shared_state():
    """Read the shared-state file, re-parsing only when it has actually changed.

    Several add-ins call this from their poll handlers, so it ran at the combined
    poll rate — an open + parse each time for a file that changes on user action.
    The stamp is (mtime_ns, size) from a stat, so writes from the OTHER add-in
    processes are still picked up; only the redundant re-parse is skipped.
    """
    global _shared_cache
    try:
        st = os.stat(_SHARED_STATE_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _shared_cache = None
        return {}
    # Callers are free to mutate what they get back (several read-modify-write it),
    # so hand out a copy — the values are all flat scalars, so a shallow one is enough.
    if _shared_cache is not None and _shared_cache[0] == stamp:
        return dict(_shared_cache[1])
    try:
        with open(_SHARED_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        _shared_cache = (stamp, data)
        return dict(data)
    except Exception:
        return {}


def _write_shared_state(data):
    global _shared_cache
    try:
        with open(_SHARED_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        _shared_cache = None      # force a re-read; our own write moved the stamp
    except Exception:
        pass


def isolate_is_active():
    """Isolation is now always on — you are always isolated to wherever you are.

    Kept as a function (rather than removing it) so the many callers in the nav
    add-ins and the Browser don't have to change.  At root/Top Level there is
    nothing to isolate, so the isolate calls are harmless no-ops there.
    """
    return True


# ── Global folder-visibility policy ───────────────────────────────────────────
#
# Folder visibility is recomputed by apply_policy() every time the active
# component changes (navigation, manual browser activation, new component,
# document open).  See apply_policy's docstring for the rules.  The only
# remaining user toggle is bodies (Toggle Body Visibility); sketches and origins
# are now fully automatic.

# bodies_visible / sketches_visible now mirror the TRANSIENT per-activation
# flags (body_shown / sketch_shown) that apply_policy maintains, so the Browser
# sync indicators reflect what is actually shown for the active component.  They
# are recomputed (and reset to the depth-based default) on every activation.
def bodies_visible():
    return bool(_read_shared_state().get('body_shown', True))   # bodies default ON


def sketches_visible():
    return bool(_read_shared_state().get('sketch_shown', False))


# origins are always hidden at the folder level (sketch-edit axis reveal aside).
def origins_visible():
    return False


def origin_folder_wanted(comp_id):
    """Should THIS component's Origin folder be open, outside of a live sketch edit?

    True only for a component the Browser's Origin group turned an entity on in
    (Browser.py's _mark_origins_shown / origins_shown) — everything else stays the
    policy default of closed. Read fresh each call, same reasoning as every other
    _read_shared_state() call here: cheap, and another add-in process's write shows
    up on the very next tick."""
    try:
        return comp_id in (_read_shared_state().get('origins_shown') or [])
    except Exception:
        return False


# isolate_off mirrors the TRANSIENT per-activation un-isolate flag that
# toggle_isolate maintains.  True == the user has temporarily un-isolated the
# active component (siblings/ancestors shown); resets to False (isolated) on
# every activation, exactly like body_shown/sketch_shown.
def isolate_off_active():
    return bool(_read_shared_state().get('isolate_off', False))


# ── Body fallback one level up ────────────────────────────────────────────────
# When bodies are toggled on but nothing in the focus subtree has a visible body, the
# body toggle acts one level up instead: the parent's own bodies and every sibling
# subtree's. apply_policy derives this on every pass (`body_up` in the shared state,
# read by the Browser) and widens the isolation onto the parent while it lasts
# (`widened_for`, below). Sketches never fall back: while isolated, only the focus
# component's OWN sketches ever show.

def isolate_parent_occ(occ):
    """occ's nearest ancestor that is not a pass-through wrapper (see DISREGARD_TAG).
    None when that is the root."""
    try:
        cur = occ.assemblyContext if occ else None
        while cur is not None and is_disregarded(cur):
            cur = cur.assemblyContext
        return cur
    except Exception:
        return None


def fallback_active(kind):
    """True while the 'body' toggle is acting one level up ('sketch' never does)."""
    return bool(_read_shared_state().get(kind + '_up', False))


def _visible_subtree_comps(focus_occ):
    """focus_occ's component plus every component under it reached through lit
    occurrences — what the focus subtree can actually show."""
    comps = [focus_occ.component]
    stack = list(focus_occ.childOccurrences)
    while stack:
        occ = stack.pop()
        try:
            if not occ.isLightBulbOn:
                continue
            comps.append(occ.component)
            stack.extend(occ.childOccurrences)
        except Exception:
            pass
    return comps


def _has_visible_leaf(comps, kind, hidden, groups, skip_sketches=()):
    """Does any of comps hold a body (kind 'bd') / sketch ('sk') the leaf policy
    shows? skip_sketches: sketches that don't count — the one being edited always
    shows, so it can't be what the sketch toggle reveals."""
    for comp in comps:
        cid = _comp_id(comp)
        try:
            if kind == 'bd':
                for k, b in _keyed(cid, 'bd', comp.bRepBodies):
                    if k and k not in hidden and not groups.get(k):
                        return True
            else:
                for k, sk in _keyed(cid, 'sk', comp.sketches):
                    if _is_bounds_sketch(sk) or _is_master_sketch(sk):
                        continue
                    if any(_same_sketch(sk, x) for x in skip_sketches):
                        continue
                    if k and k not in hidden and not groups.get(k):
                        return True
        except Exception:
            pass
    return False


def _kind_would_show(design, kind, isolated=None):
    """Would turning the 'body' / 'sketch' toggle on show anything?  Sketches: the
    focus component's own only (every sketch when un-isolated).  Bodies: the focus
    subtree, or else the level-up fallback apply_policy would switch to — judged by
    every component under the parent regardless of occurrence bulbs, since isolation
    has the siblings switched off at this point.  isolated: judge as isolated / not
    (default: the current un-isolate flag)."""
    k = 'bd' if kind == 'body' else 'sk'
    groups = hidden_group_members(_lineage(design))
    hidden = load_hidden(design)
    if (not isolated) if isolated is not None else isolate_off_active():
        return _has_visible_leaf(list(design.allComponents), k, hidden, groups)
    focus = view_occ(design)             # a suspend-mode peek is where the toggles act
    if k == 'sk':
        comp = focus.component if focus is not None else design.rootComponent
        return _has_visible_leaf([comp], k, hidden, groups)
    if focus is None:
        root = design.rootComponent
        comps = [root]
        for occ in root.occurrences:
            try:
                if occ.isLightBulbOn:
                    comps += _visible_subtree_comps(occ)
            except Exception:
                pass
        return _has_visible_leaf(comps, k, hidden, groups)
    if _has_visible_leaf(_visible_subtree_comps(focus), k, hidden, groups):
        return True
    parent = isolate_parent_occ(focus)
    pcomp = parent.component if parent is not None else design.rootComponent
    comps = [pcomp] + [o.component for o in pcomp.allOccurrences]
    return _has_visible_leaf(comps, k, hidden, groups)


def _edit_sketch_toggle_matters(design):
    """While a sketch is open, would the Sketch toggle change anything?  It only
    gates the OTHER sketches of the component being edited (plus, in a master
    session, the original sketch's neighbours — see apply_policy), so with none of
    those there is nothing for it to show or hide.  An unidentified edited sketch
    answers yes: every sketch is kept visible then, and guessing wrong costs a step."""
    editing, edit_native, _tok = _edit_sketch_info(design)
    if not editing or edit_native is None:
        return True
    groups = hidden_group_members(_lineage(design))
    hidden = load_hidden(design)
    comps, skip = [design.activeComponent], [edit_native]
    companion = master_session_editing(design, edit_native)
    if companion:
        comps.append(companion[0].parentComponent)
        skip.append(companion[0])
    return _has_visible_leaf(comps, 'sk', hidden, groups, skip_sketches=skip)


def _clear_widened():
    state = _read_shared_state()
    if state.get('widened_for'):
        state['widened_for'] = ''
        _write_shared_state(state)


def _widen_to_parent(design, focus, keep=None):
    """Isolate onto focus's parent, then restore the saved snapshot at every level from
    the parent down to the focus, so the siblings come back on (minus anything the user
    hid) — including siblings lifted out of a pass-through wrapper between the two. The
    focus's own subtree is left alone. A root parent un-isolates the top level."""
    parent = isolate_parent_occ(focus)
    if parent is not None:
        isolate_occurrence_no_snapshot(design, parent, keep=keep)
    else:
        restore_snapshot(design, None)
    parent_path = _occ_path_str(parent) if parent is not None else None
    levels = []
    cur = focus.assemblyContext
    while cur is not None:
        levels.append(cur)
        if _occ_path_str(cur) == parent_path:
            break
        cur = cur.assemblyContext
    for occ in reversed(levels):
        restore_snapshot(design, occ)
    state = _read_shared_state()
    state['widened_for'] = _occ_path_str(focus)
    _write_shared_state(state)
    _log('WIDEN', 'focus=' + focus.name, 'onto=' + (parent.name if parent else 'root'))


def _active_occ(design):
    """Best-effort current active occurrence (None when root/Top Level is active)."""
    active_comp = design.activeComponent
    recalled = recall_active_occ(design)
    if recalled and recalled.component == active_comp:
        return recalled
    return find_occurrence(design, active_comp)


def _comp_id(comp):
    """Stable, hashable key for a component (adsk Component objects are unhashable)."""
    try:
        return comp.id
    except Exception:
        try:
            return comp.name
        except Exception:
            return None


def _ancestor_ids(active_occ):
    """Set of component ids that are STRICT ancestors of active_occ."""
    out = set()
    try:
        cur = active_occ.assemblyContext if active_occ else None
        while cur is not None:
            out.add(_comp_id(cur.component))
            cur = cur.assemblyContext
    except Exception:
        pass
    return out


def _descendant_ids(active_comp):
    """Set of component ids nested anywhere under active_comp (excl. itself)."""
    out = set()
    try:
        for occ in active_comp.allOccurrences:
            out.add(_comp_id(occ.component))
    except Exception:
        pass
    return out


def _is_leaf(comp):
    """True when comp is a BOTTOM-LEVEL component — it has no direct child
    components.  Leaf components show their sketches + bodies by default; non-leaf
    (assembly-like) components hide both by default (you reveal them temporarily
    via the toggles).  Component.occurrences is the list of direct child
    occurrences, so count == 0 means a true leaf."""
    try:
        return comp.occurrences.count == 0
    except Exception:
        return True


def is_editing_sketch(design):
    """True when a sketch is currently being edited.

    While a sketch is open, the sketch-edit handler (in Go Down a Level) owns the
    active component's origin folder + axis bulbs to reveal the two in-plane axes.
    apply_policy must NOT stomp those, so it leaves the origin folder untouched
    during a sketch edit.

    Detection does NOT use design.activeEditObject: for a sketch nested inside an
    occurrence (the common case once you've descended into a sub-component) that
    property raises InternalValidationError, which would silently misreport "not
    editing".  The SketchStop command is enabled iff a sketch is open, never
    raises, and works at any depth — so it is the authoritative signal.
    """
    try:
        if adsk is None:
            return False
        ui = adsk.core.Application.get().userInterface
        cd = ui.commandDefinitions.itemById('SketchStop')
        if cd is not None:
            # ...with one exception: it lags the OPENING of a sketch by a beat, and
            # plenty runs in that beat — apply_policy from the very handler that
            # opened it, and the Browser sync tick that tells the window whether a
            # sketch is being edited. Both concluded "not editing", so the sketch you
            # had just opened was switched off (by the policy's hidden set, and by the
            # window's hidden-group enforcement) and nothing switched it back on: you
            # were editing an invisible sketch. See edit_is_settling().
            # ...and it lags the CLOSING too: SketchStop stays enabled through the
            # exit handlers and the nav add-ins' deferred re-activation, whose
            # apply_policy then took the "a sketch is open, can't tell which" path
            # and switched every sketch on. See exit_is_settling().
            enabled = bool(cd.controlDefinition.isEnabled)
            if exit_is_settling(enabled):
                return False
            return enabled or edit_is_settling()
    except Exception:
        pass
    # Fallback (older Fusion / no UI): try the edit object directly.
    try:
        return adsk is not None and adsk.fusion.Sketch.cast(design.activeEditObject) is not None
    except Exception:
        return False


# Six other add-ins call this; the leading underscore said "private" and was never
# true. Kept so nothing breaks on a partial reload.
_is_editing_sketch = is_editing_sketch


def active_edit_sketch(design):
    """The sketch currently open for edit, or None. Correct AT ANY DEPTH.

    THE one way to answer "which sketch is open". Reading design.activeEditObject
    yourself is the wrong way and has cost real bugs: it raises
    InternalValidationError for a sketch nested inside an occurrence — the normal
    case once you have descended into a sub-component — so `Sketch.cast(...)`
    either blew up the enclosing handler or, wrapped in a bare except, quietly
    reported "no sketch open" at exactly the depths where one usually is.

    Returns the sketch in its edit context where Fusion gives us one (so
    .assemblyContext and proxied geometry are usable); callers wanting the
    definition should take .nativeObject themselves. None means either no sketch
    is open OR one is open but nothing recorded which — use is_editing_sketch()
    to tell those apart when it matters.
    """
    if adsk is None or design is None:
        return None
    try:
        sk = adsk.fusion.Sketch.cast(design.activeEditObject)
    except Exception:
        sk = None                       # nested-in-occurrence: the recall below answers
    if not sk:
        sk = recall_edit_sketch(design)
    return sk


def edited_sketch_from_marker(design):
    """Identify the sketch currently being edited from the timeline marker.

    When a sketch edit begins Fusion rolls the timeline marker to immediately
    after that sketch's node, so the item just before the marker is the edited
    sketch.  This is entry-method-independent — unlike reading the selection at
    SketchActivate-starting (which misses double-click entry, where Fusion has
    already cleared the selection by the time the event fires) and unlike
    design.activeEditObject (which raises for a sketch nested in an occurrence).

    MUST be called BEFORE restore_pre_sketch_marker rolls the marker forward.
    Returns the native Sketch, or None."""
    if adsk is None:
        return None
    try:
        tl  = design.timeline
        pos = tl.markerPosition
        if pos <= 0 or pos > tl.count:
            return None
        it  = tl.item(pos - 1)
        sk  = adsk.fusion.Sketch.cast(it.entity)
        return (sk.nativeObject or sk) if sk else None
    except Exception:
        return None


def _edit_sketch_info(design):
    """Return (editing_sketch, edit_native, edit_token) for the active sketch
    edit, or (False, None, '') when not editing a sketch.

    edit_native may be None even while editing_sketch is True — that means a
    sketch IS open but we could not identify WHICH one (e.g. it was entered by a
    path that never recorded it).  Callers must treat (True, None, '') as "keep
    every sketch visible" so the sketch being edited is never accidentally
    hidden."""
    if not is_editing_sketch(design):
        return False, None, ''
    sk = active_edit_sketch(design)
    if not sk:
        return True, None, ''
    native = sk.nativeObject or sk
    try:
        token = native.entityToken
    except Exception:
        token = ''
    return True, native, token


# ── Master edit session ───────────────────────────────────────────────────────
#
# Edit Original (Master Sketches add-in) leaves the sketch you are in, opens the
# master it projects from, and later brings you back. That is ONE move made of two
# native commands each way (SketchStop + SketchActivate), and every nav handler used
# to react to each of them as if you had left or entered a sketch yourself: the exit
# restores switched every sketch back on, the Browser's jump-down followed the
# master's selection to the root and un-isolated the whole model, and the Master
# add-in's own return fired on the wrong SketchStop. Slow, and everything flashed.
#
# So the session is recorded HERE, where every one of those handlers already looks,
# and they ask it instead of each keeping a private guess:
#
#   master_hop_in_progress()   the SketchStop/SketchActivate in flight are a hop, not
#                              you leaving a sketch — skip the exit visuals.
#   master_session_live()      a hop, or the master open for its session — no
#                              jump-down / click-descend (either would leave it).
#   request_master_return()    exit commands (Go Up, Go to Top, a Browser activation)
#                              ask first; with the master open they go back to the
#                              ORIGINAL sketch instead (MasterReturnCmd does the work).
#   master_session_on_activate()  Go Down's SketchActivate handler: phase changes.
#   apply_policy               with the master open, the original sketch's occurrence
#                              is the FOCUS (isolation + the toggles act around it) and
#                              the original sketch always shows.
#
# Phases: entering (hop in) → editing → returning (hop out) → gone. A hop phase is
# trusted for HOP_TIMEOUT_S only, so one that dies half-way can never leave the exit
# handlers switched off. A session whose master is no longer open is dropped by the
# next thing that asks (see request_master_return / master_session_on_activate).

MASTER_SESSION_KEY   = 'master_session'
MASTER_RETURN_CMD_ID = 'MasterReturnCmd'
HOP_TIMEOUT_S        = 60.0
_HOP_PHASES          = ('entering', 'returning')
# The transient view state in force in the original sketch, put back on return so
# you land exactly as you left (toggles included), not at the sketch-entry defaults.
_SAVED_TRANSIENTS = ('sketch_shown', 'body_shown', 'sketches_visible', 'bodies_visible',
                     'policy_ctx', 'body_ctx')


def begin_master_session(design, master, origin_sketch, origin_occ):
    """Record a hop from origin_sketch (open now, reached through origin_occ — None
    on the root) into `master`. Call BEFORE queuing the SketchStop that starts it."""
    try:
        m = getattr(master, 'nativeObject', None) or master
        o = getattr(origin_sketch, 'nativeObject', None) or origin_sketch
        state = _read_shared_state()
        state[MASTER_SESSION_KEY] = {
            'phase':  'entering',
            'since':  _time.time(),
            'doc':    _lineage(design),
            'master': {'token': m.entityToken, 'name': m.name},
            'origin': {'token': o.entityToken, 'name': o.name,
                       'occ': _occ_path_str(origin_occ) if origin_occ else ''},
            'saved':  {k: state[k] for k in _SAVED_TRANSIENTS if k in state},
            # Un-isolated (Toggle Isolation) in the original sketch: the master view
            # starts isolated regardless, so it is re-isolated on the way in.
            'was_unisolated': bool(state.get('isolate_off', False)),
        }
        _write_shared_state(state)
        _log('MASTER_SESSION begin', 'master=' + m.name, 'origin=' + o.name,
             'occ=' + (_occ_path_str(origin_occ) if origin_occ else 'root'))
        return True
    except Exception as _e:
        _log('MASTER_SESSION begin ERROR', _e, _traceback.format_exc())
        return False


def master_session(design=None):
    """The session record, or None. A record for another document is ignored."""
    s = _read_shared_state().get(MASTER_SESSION_KEY)
    if not isinstance(s, dict):
        return None
    if design is not None and s.get('doc') != _lineage(design):
        return None
    return s


def set_master_phase(phase):
    state = _read_shared_state()
    s = state.get(MASTER_SESSION_KEY)
    if not isinstance(s, dict):
        return
    s['phase'] = phase
    s['since'] = _time.time()
    _write_shared_state(state)
    _log('MASTER_SESSION phase=' + phase)


def end_master_session(reason=''):
    state = _read_shared_state()
    if state.pop(MASTER_SESSION_KEY, None) is not None:
        _write_shared_state(state)
        _log('MASTER_SESSION end', reason)


SKETCH_HOP_KEY       = 'sketch_hop'
SKETCH_HOP_TIMEOUT_S = 10.0


def begin_sketch_hop(reason=''):
    """A hop with no session to come back to (Section Sketch's Go to Source): the
    SketchStop + SketchActivate about to be queued move you from one sketch straight
    into another, and every exit handler treats it as master_hop_in_progress. Ended
    by the next sketch opening (master_session_on_activate) or SKETCH_HOP_TIMEOUT_S."""
    state = _read_shared_state()
    state[SKETCH_HOP_KEY] = {'since': _time.time(), 'why': reason}
    _write_shared_state(state)
    _log('SKETCH_HOP begin', reason)


def end_sketch_hop(reason=''):
    state = _read_shared_state()
    if state.pop(SKETCH_HOP_KEY, None) is not None:
        _write_shared_state(state)
        _log('SKETCH_HOP end', reason)


def master_hop_in_progress():
    """True while Edit Original's SketchStop/SketchActivate pair is in flight — or
    a plain sketch hop's (begin_sketch_hop)."""
    h = _read_shared_state().get(SKETCH_HOP_KEY)
    if isinstance(h, dict) and _time.time() - float(h.get('since') or 0) < SKETCH_HOP_TIMEOUT_S:
        return True
    s = master_session()
    if not s or s.get('phase') not in _HOP_PHASES:
        return False
    return (_time.time() - float(s.get('since') or 0)) < HOP_TIMEOUT_S


def _same_sketch(a, b):
    """Identity, token, or name within the same component — any one is enough (a
    proxy/native or token mismatch must not make the same sketch look different)."""
    if a is None or b is None:
        return False
    a = getattr(a, 'nativeObject', None) or a
    b = getattr(b, 'nativeObject', None) or b
    try:
        if a == b:
            return True
    except Exception:
        pass
    try:
        if a.entityToken == b.entityToken:
            return True
    except Exception:
        pass
    try:
        return a.name == b.name and a.parentComponent == b.parentComponent
    except Exception:
        return False


def _session_sketch(design, rec, comp=None):
    """Resolve a {'token','name'} record to a native Sketch (name fallback in comp)."""
    try:
        ents = design.findEntityByToken(rec.get('token') or '')
        sk = adsk.fusion.Sketch.cast(ents[0]) if ents else None
        if sk:
            return sk.nativeObject or sk
    except Exception:
        pass
    if comp is not None:
        try:
            for sk in comp.sketches:
                if sk.name == rec.get('name'):
                    return sk
        except Exception:
            pass
    return None


def master_session_sketches(design, s=None):
    """(master, origin, origin_occ) for the session — natives, origin_occ None when
    the original sketch is on the root. Any of them None when it no longer resolves."""
    s = s or master_session(design)
    if not s:
        return None, None, None
    root = design.rootComponent
    master = _session_sketch(design, s.get('master') or {}, root)
    origin_rec = s.get('origin') or {}
    occ = _occ_from_path_str(design, origin_rec.get('occ') or '')
    origin = _session_sketch(design, origin_rec, occ.component if occ else root)
    if origin is not None:
        try:
            comp = origin.parentComponent
            if comp == root:
                occ = None
            elif occ is None or occ.component != comp:
                occ = find_occurrence(design, comp)
        except Exception:
            pass
    return master, origin, occ


def master_session_editing(design, edit_native=None):
    """(origin, origin_occ) when the session's master is the sketch open now — the
    one state in which the original sketch is the view's focus — else None."""
    s = master_session(design)
    if not s or s.get('phase') != 'editing':
        return None
    if edit_native is None:
        editing, edit_native, _tok = _edit_sketch_info(design)
        if not editing or edit_native is None:
            return None
    master, origin, occ = master_session_sketches(design, s)
    if master is None or origin is None or not _same_sketch(edit_native, master):
        return None
    return origin, occ


def master_session_live(design):
    """A hop is in flight, or the master is open for its session. Anything that would
    activate a component on its own initiative (the Browser's jump-down, Click To
    Descend) must stand back: activating a component leaves the sketch."""
    try:
        return master_hop_in_progress() or master_session_editing(design) is not None
    except Exception:
        return False


def master_session_on_activate(design, sketch):
    """Go Down's SketchActivate-terminated handler reports every sketch that opens.
    Call it AFTER enter_sketch_body_default and BEFORE apply_policy:

      • the master, while entering (or re-entered while editing — Flush Undo stops
        and re-opens the sketch)            → editing
      • the original sketch, while returning → the pre-hop view state goes back,
                                               session over
      • anything else                        → session over (you went elsewhere)

    Any sketch opening also ends a plain sketch hop (begin_sketch_hop).
    """
    if sketch is not None:
        end_sketch_hop('opened ' + getattr(sketch, 'name', '?'))
    s = master_session(design)
    if not s:
        return
    try:
        native = (sketch.nativeObject or sketch) if sketch else None
        phase = s.get('phase')
        if native is None:
            if phase in _HOP_PHASES:
                return                     # can't tell which — let the hop finish
            end_master_session('unidentified sketch opened')
            return
        master, origin, occ = master_session_sketches(design, s)
        if phase in ('entering', 'editing') and _same_sketch(native, master):
            if phase == 'entering':
                if s.get('was_unisolated') and occ is not None:
                    isolate_occurrence_no_snapshot(design, occ)
                set_master_phase('editing')
            return
        if phase == 'returning' and _same_sketch(native, origin):
            # The return re-isolated onto the original occurrence (Master Sketches'
            # _finish_return), so the flag says isolated too.
            state = _read_shared_state()
            state.update(s.get('saved') or {})
            state['isolate_off'] = False
            state.pop(MASTER_SESSION_KEY, None)
            _write_shared_state(state)
            _log('MASTER_SESSION end', 'returned to ' + native.name)
            return
        end_master_session('another sketch opened: ' + native.name)
    except Exception as _e:
        _log('MASTER_SESSION on_activate ERROR', _e, _traceback.format_exc())


def request_master_return(design):
    """For every command that exits a sketch: returns True when it has been handled
    here and the caller must do NOTHING else.

    With the session's master open, that means going back to the original sketch
    (MasterReturnCmd, owned by the Master Sketches add-in). During a hop it means
    ignoring the press — the hop is already moving you. Otherwise False, and a
    session left behind (its master closed some other way) is dropped."""
    try:
        if master_hop_in_progress():
            _log('MASTER_SESSION exit ignored mid-hop')
            return True
        if master_session_editing(design) is None:
            if master_session(design):
                end_master_session('master no longer open at exit')
            return False
        ui = adsk.core.Application.get().userInterface
        cd = ui.commandDefinitions.itemById(MASTER_RETURN_CMD_ID)
        if cd is None:
            return False                   # Master Sketches not running
        cd.execute()
        _log('MASTER_SESSION exit → return to original')
        return True
    except Exception as _e:
        _log('MASTER_SESSION request_return ERROR', _e, _traceback.format_exc())
        return False


def _focus_occ(design):
    """The occurrence the view is centred on: the original sketch's while its master
    is open (see the master-session section), otherwise the active one."""
    hit = master_session_editing(design)
    if hit is not None:
        return hit[1]
    return _active_occ(design)


# ── Suspend mode: peek ────────────────────────────────────────────────────────
# The Browser's suspend mode (fusion_browser/CLAUDE.md § Suspend mode) lets you move
# around the model while a command or a sketch is open, without leaving it: the
# active component, the open sketch and the running command never change. Moving
# is a PEEK — visibility only, no camera: the view isolates onto where you went,
# and the real active path stays lit beside it (isolate_occurrence_no_snapshot's
# keep). apply_policy treats the peek as the FOCUS (the slot a master session's
# original sketch uses) and as the toggles' context: arriving resets sketch_shown /
# body_shown to the peek's arrival defaults (bodies; sketches only where it has no
# bodies), and Cycle Visibility acts on the peek AND the active component (a toggle
# is written into the parked peek_home too — _home_follow — so the component you are
# in follows it and keeps it when the peek ends; arriving at a peek resets only the
# live copy). Dim Outside Active still reads the real
# active component — so what you peek at draws dimmed, and inside a sketch every body
# does.
#
# Shared State.json:  suspend    bool
#                     peek       occurrence path, '' = the root; absent = no peek
#                     peek_home  the focus's toggle state, parked while peeking
#
#   is_suspended()           the nav add-ins ask this first, and peek instead
#   view_occ(design)         "where you are" for them: the peek, else the focus
#   peek(design, occ)        isolate onto occ (None = root), active kept lit; onto
#                            the focus itself, ends the peek (still suspended)
#   set_suspended(d, on)     off clears the peek, puts the parked toggles back and
#                            re-isolates onto the focus — the jump back
#   clear_peek()             a REAL activation made while suspended (the Browser's
#                            escape hatch) supersedes the peek
#
# The Browser add-in owns the flag; it also shows the edge glow (bin/fusion_glow.m).

def is_suspended():
    return bool(_read_shared_state().get('suspend'))


def _peek_target(design):
    """(True, occ-or-None-for-root) while suspended with a peek set that still
    resolves, else (False, None)."""
    st = _read_shared_state()
    if not st.get('suspend') or not isinstance(st.get('peek'), str):
        return False, None
    p = st['peek']
    if p == '':
        return True, None
    occ = _occ_from_path_str(design, p)
    return (True, occ) if occ is not None else (False, None)


def view_occ(design):
    """The occurrence the view is on: the peek while one is set, else the focus
    (_focus_occ). None = the root. Nav computes up / down from here while suspended."""
    has, occ = _peek_target(design)
    return occ if has else _focus_occ(design)


def _occ_chain(occ):
    """[top-level … occ], or [] for None."""
    out = []
    while occ is not None:
        out.append(occ)
        try:
            occ = occ.assemblyContext
        except Exception:
            break
    return out[::-1]


def _reisolate(design, frm, target, keep=None):
    """Browser._activate_target_inner's un-isolate / re-isolate dance, minus the
    activation: undo isolation (ledger bulbs, snapshot folders) from `frm` and every
    level on `keep`'s path, then isolate onto `target` with `keep` lit beside it.
    target None = the root: nothing is isolated then. Buffered, like the Browser's."""
    take_snapshot(design, frm)
    levels, seen = [], set()
    for o in _occ_chain(frm) + _occ_chain(keep):
        p = _occ_path_str(o)
        if p not in seen:
            seen.add(p)
            levels.append(o)
    with bulb_batch():
        if unisolate_from_ledger(design):
            restore_folders_only(design, None)
            for o in levels:
                restore_folders_only(design, o)
        else:
            restore_snapshot(design, None)
            for o in levels:
                restore_snapshot(design, o)
        if target is not None:
            isolate_occurrence_no_snapshot(design, target, keep=keep)


def is_peeking(design):
    return _peek_target(design)[0]


# The peek is the toggles' context (apply_policy): arriving at one resets sketch_shown /
# body_shown to its arrival defaults, and Cycle Visibility then acts on what you are
# looking at. The focus's own values are parked here on the first peek — the focus
# keeps drawing with them meanwhile — and put back when the peek ends. Peeking itself
# never changes them; a toggle / cycle made while peeking does (_home_follow).
_HOME_KEYS = ('sketch_shown', 'body_shown', 'policy_ctx', 'body_ctx', 'isolate_off',
              'sketches_visible', 'bodies_visible')


def _home_toggles(st):
    """(sketch_shown, body_shown) the focus had when the peek began."""
    home = st.get('peek_home') or {}
    return bool(home.get('sketch_shown', True)), bool(home.get('body_shown', True))


def _home_follow(st, key, value):
    """A toggle / cycle made while peeking reaches the focus too: write it into the
    parked copy as well, so the active component draws with it and keeps it when the
    peek ends. Arriving at a peek still resets only the live copy (not written)."""
    home = st.get('peek_home')
    if isinstance(home, dict):
        home[key] = value
        home[_MIRROR_KEY[key]] = value


def _restore_home(st):
    """Put the focus's parked toggle state back into `st` (not written)."""
    home = st.pop('peek_home', None)
    if isinstance(home, dict):
        for k in _HOME_KEYS:
            if k in home:
                st[k] = home[k]
            else:
                st.pop(k, None)


def peek(design, target):
    """Look at `target` (an Occurrence, None = the root) without activating it.
    A peek onto the focus itself ends the peek — you are back where you are."""
    try:
        focus = _focus_occ(design)
        if _occ_path_str(target) == _occ_path_str(focus) and not is_peeking(design):
            return True                   # already looking at where you are
        with one_step('Navigate'):
            if _occ_path_str(target) == _occ_path_str(focus):
                _reisolate(design, view_occ(design), focus)
                st = _read_shared_state()
                st['suspend'] = True
                st.pop('peek', None)
                _restore_home(st)
                _write_shared_state(st)
                apply_policy(design)
                _log('PEEK', 'home')
                return True
            _reisolate(design, view_occ(design), target, keep=focus)
            st = _read_shared_state()
            if 'peek_home' not in st:
                st['peek_home'] = {k: st[k] for k in _HOME_KEYS if k in st}
            st['suspend'] = True
            st['peek'] = _occ_path_str(target) if target is not None else ''
            st['isolate_off'] = False     # the peek just isolated, whatever it was before
            _write_shared_state(st)
            apply_policy(design)
        _log('PEEK', target.name if target is not None else 'root')
        return True
    except Exception as _e:
        _log('PEEK ERROR', _e, _traceback.format_exc())
        return False


def peek_up(design):
    """Go Up, as a peek: the parent of the view, stepping over pass-through
    wrappers. False when the view is already the root."""
    v = view_occ(design)
    if v is None:
        return False
    return peek(design, isolate_parent_occ(v))


def peek_toward(design, target, full=False):
    """Go Down (one level) / Go to Bottom (full), as a peek: from the view toward
    `target`, stepping over wrappers as Go Down does. False when the view is
    already there (or below), so there is nothing to move to."""
    if target is None:
        return False
    _, path = _find_occ_info(design, target)
    if not path:
        return False
    v = view_occ(design)
    vpath = _occ_path_str(v) if v is not None else ''
    tpath = _occ_path_str(target)
    if vpath and (vpath == tpath or vpath.startswith(tpath + '/')):
        return False
    if full:
        return peek(design, target)
    k = -1
    for i, o in enumerate(path):
        p = _occ_path_str(o)
        if vpath and (vpath == p or vpath.startswith(p + '/')):
            k = i
    nxt = k + 1
    while nxt < len(path) - 1 and is_disregarded(path[nxt]):
        nxt += 1
    return peek(design, path[nxt] if nxt < len(path) else target)


def clear_peek():
    """A real activation supersedes the peek — and the parked toggles, which belonged
    to the component it is leaving (the activation resets them anyway)."""
    st = _read_shared_state()
    if 'peek' in st or 'peek_home' in st:
        st.pop('peek', None)
        st.pop('peek_home', None)
        _write_shared_state(st)


def set_suspended(design, on):
    """Suspend on / off. Off with a peek set re-isolates onto the focus (the active
    component, or a master session's original) and re-applies the policy."""
    try:
        has, occ = _peek_target(design) if design is not None else (False, None)
        st = _read_shared_state()
        if bool(st.get('suspend')) == bool(on) and (on or not (
                'peek' in st or 'peek_home' in st)):
            return
        st['suspend'] = bool(on)
        if not on:
            st.pop('peek', None)
            _restore_home(st)
        _write_shared_state(st)
        _log('SUSPEND', 'on' if on else 'off', 'peek=' + (
            (occ.name if occ is not None else 'root') if has else '-'))
        if not on and has:
            with one_step('Navigate'):
                _reisolate(design, occ, _focus_occ(design))
                apply_policy(design)
    except Exception as _e:
        _log('SUSPEND ERROR', _e, _traceback.format_exc())


# ── Leaf visibility: one authority, declared intent ───────────────────────────
#
# A sketch's or body's bulb used to be decided by READING it: apply_policy folded
# whatever the bulbs happened to say into a "the user hid this" set.  A bulb cannot
# say WHY it is off, so every hide the Browser's groups performed was filed as a
# deliberate one — in one live document 17 of the 22 recorded "user hides" were
# group hides wearing a user hide's clothes.  The Browser meanwhile kept a second
# copy of the same information (a per-group `snap` of what each member "was set
# to"), re-imposed on a poll with a retry timer, and the two authorities disagreed.
#
# Bulbs are now DERIVED, from three inputs and nothing else:
#
#     bulb(leaf) = (leaf not in INTENT) and (leaf not in a hidden GROUP)
#                  [the sketch-edit override aside, while a sketch is open]
#
#   INTENT  — `hidden`: the leaf keys the user has actually hidden.  Written only
#             by an explicit gesture (⌘E, a Browser row's own bulb) or by adopting
#             a change made outside our code.  Never inferred from a bulb.
#   GROUPS  — the Browser's virtual folders (folders.json).  A group's hide stays
#             ONE FLAG ON THE GROUP; it is never copied onto its members, so it can
#             never be mistaken for a hide of a member.
#   MIRROR  — `mirror`: the value this code last wrote to each bulb.  It is what
#             makes outside changes adoptable without guessing — a bulb still equal
#             to the mirror is our own policy and says nothing, while one that has
#             drifted from it was written by something else (Fusion's own browser,
#             the Hide command, an undo, a feature consuming a sketch), and THAT is
#             a genuine statement of intent.  Same principle as the isolate ledger
#             above: record what we did, so we can subtract it.
#
# All three are keyed per document LINEAGE, not per version.  The old per-version
# store started a fresh record on every save and _prune dropped it five versions
# later; it survived only because the bulbs were re-read each time.  With the
# guessing gone the record has to be durable.

_LEAF_STORE  = '__leaves__'      # top-level key, outside the per-version doc keys
_GROUPS_FILE = '/Users/josh/Code/Scripts/fusion_browser/folders.json'


def _lineage(design):
    """The document's LINEAGE id — the same key the Browser's folders.json uses.

    Deliberately not _doc(), which is the versioned name ("260612 v312")."""
    try:
        doc = design.parentDocument
    except Exception:
        return '__unknown__'
    try:
        df = doc.dataFile
        if df and df.id:
            return df.id
    except Exception:
        pass
    try:
        return '__unsaved__:' + (doc.name or '')
    except Exception:
        return '__unknown__'


def _leaf_key(comp_id, kind, name, nth=1):
    """Durable identity for a sketch or body: component id, kind, name.

    NOT the entityToken the Browser used to persist its group membership with —
    23 of 159 memberships in one live document had already stopped resolving, and
    each dead one is an item that silently left its group.  A name survives
    versions, saves and reopens — but it is NOT unique: Fusion lets two bodies in
    one component share a name exactly (12 pairs of 'Ribs… (1)' in 260612), and
    one key for both filed, hid and grouped them as one.  So a REPEAT of a name is
    told apart by its place among the same-named, in the collection's order:
    '<cid>|bd|Ribs (1)', then '…#2', '…#3'.  The first keeps the plain key, so every
    record made before this still means what it meant.  Loops key through
    _keyed(); one entity on its own through leaf_key_for().

    A BODY with an id stamped on it (stamp_bodies) is keyed by that instead — see
    _uid_key.  The '#n' above is only its place in comp.bRepBodies, and that order
    shifts when the timeline recomputes: twins swapped keys, and their groups with
    them (2026-10-06, Ribs' Depth/Width groups)."""
    key = '{}|{}|{}'.format(comp_id or '', kind, name or '')
    return key if nth <= 1 else '{}#{}'.format(key, nth)


# A body's own id: an attribute, so it lives in the document — it survives renames,
# recomputes, saves and reopens, and two bodies with one name still have two ids.
# Written only by stamp_bodies, on a Browser gesture (filing a body into a group):
# an attribute write is an undo step, and stamping on every refresh would make ⌘Z
# undo a stamp that the next refresh put straight back — the user could never undo
# past it.  An unstamped body keeps its name key until then.
_UID_GROUP = 'Browser'
_UID_NAME = 'uid'
_UID_MARK = '@'          # '<cid>|bd|@<id>' — '@' never starts a name key we write


def _uid_key(comp_id, uid):
    return '{}|bd|{}{}'.format(comp_id or '', _UID_MARK, uid)


def body_uid(body):
    """The id stamp_bodies put on this (native) body, or None."""
    try:
        a = body.attributes.itemByName(_UID_GROUP, _UID_NAME)
        return (a.value or None) if a else None
    except Exception:
        return None


def stamp_bodies(comp):
    """Give every body in comp that lacks one an id of its own.  A copied body (copy
    and paste, a pattern of bodies) comes out wearing its source's id, so the second
    holder of an id in collection order gets a fresh one too.  Returns how many were
    written.  Call inside a one_step: it is a model write."""
    import uuid
    n, seen = 0, set()
    try:
        bodies = list(comp.bRepBodies)
    except Exception:
        return 0
    for b in bodies:
        uid = body_uid(b)
        if uid and uid not in seen:
            seen.add(uid)
            continue
        uid = uuid.uuid4().hex[:12]
        try:
            b.attributes.add(_UID_GROUP, _UID_NAME, uid)
            seen.add(uid)
            n += 1
        except Exception:
            pass
    return n


def split_leaf_key(key):
    """(comp id, kind, name, nth) — _leaf_key read back. None if it isn't one."""
    try:
        cid, kind, rest = key.split('|', 2)
    except Exception:
        return None
    name, nth = rest, 1
    m = _re.match(r'^(.*)#(\d+)$', rest, _re.S)
    if m and int(m.group(2)) >= 2:
        name, nth = m.group(1), int(m.group(2))
    return cid, kind, name, nth


def _keyed(comp_id, kind, coll):
    """(key, item) for every sketch ('sk') or body ('bd') in a component's
    collection, in order — the one place a repeated name gets its '#n', so every
    caller agrees on which of two same-named items is which.  A stamped body
    yields its id key; the first holder of an id keeps it (see stamp_bodies)."""
    seen, uids = {}, set()
    for it in coll:
        try:
            name = it.name
        except Exception:
            continue
        n = seen.get(name, 0) + 1
        seen[name] = n
        uid = body_uid(it) if kind == 'bd' else None
        if uid and uid not in uids:
            uids.add(uid)
            yield _uid_key(comp_id, uid), it
        else:
            yield _leaf_key(comp_id, kind, name, n), it


def leaf_by_key(design, key):
    """The native sketch/body a leaf key names, or None."""
    parts = split_leaf_key(key)
    if not parts:
        return None
    cid, kind, name, nth = parts
    try:
        comp = design.allComponents.itemById(cid) if cid else design.rootComponent
        if comp is None:
            return None
        coll = comp.sketches if kind == 'sk' else comp.bRepBodies
        if kind == 'bd' and name.startswith(_UID_MARK):
            for k, it in _keyed(cid, kind, coll):
                if k == key:
                    return it
            return None
        n = 0
        for it in coll:
            if it.name == name:
                n += 1
                if n == nth:
                    return it
    except Exception:
        pass
    return None


def _sketch_key(comp_id, sk):
    """One sketch on its own. In a loop over a component's sketches use _keyed —
    this reads every sketch name in the component to place a repeat."""
    return leaf_key_for(sk)


# Bounds.py's SKETCH_PREFIX — the "Bounds X/Y/Z" envelope sketches it builds.
# Not imported from there: this file is loaded standalone by every nav add-in,
# and the prefix is a stable naming convention rather than something that needs
# the whole module for one string compare.
_BOUNDS_SKETCH_PREFIX = 'Bounds '
# Section.py's TETHERS_PREFIX — the hidden sketch holding the bound-tethered copies a
# user sketch projects. Same treatment as a bounds sketch: always hidden.
_TETHERS_SKETCH_PREFIX = 'Tethers '
# Section.py's REFS_PREFIX — the one sketch per component and plane holding the section
# references. Hidden like a bounds sketch, except while a sketch on its plane is open:
# then it is shown (_open_ref_sketch), or there is nothing to tie to.
_REFS_SKETCH_PREFIX = 'References '


def _open_ref_sketch(edit_native):
    """The reference sketch Section Sketch noted for the sketch being edited, or None."""
    hook = _slice_hook()
    try:
        return hook.ref_sketch(edit_native) if hook is not None and edit_native is not None \
            else None
    except Exception:
        return None


def _is_bounds_sketch(sk):
    """Is this one of Bounds.py's envelope sketches (or one of Section.py's tether
    sketches, which are treated exactly the same)?

    They are excluded from the leaf-intent system entirely (see apply_policy):
    they must default to HIDDEN always, never inherit a leaf component's
    sketches-on default, and never be "restored" back on the way the user's
    other sketches are — see _rehide_bounds_sketches for how they actually get
    hidden again."""
    try:
        return (sk.name or '').startswith((_BOUNDS_SKETCH_PREFIX, _TETHERS_SKETCH_PREFIX,
                                           _REFS_SKETCH_PREFIX))
    except Exception:
        return False


# Masters.py's NAME_PREFIX — a master sketch's name-only tell, same rationale as
# _BOUNDS_SKETCH_PREFIX above: a stable naming convention rather than something
# that needs Masters.py loaded (identity there is the attribute; the name is
# only a fast filter, and Masters.py keeps them in sync — see its docstring).
_MASTER_SKETCH_PREFIX = 'Master '


def _is_master_sketch(sk):
    """Is this one of Masters.py's master sketches?

    Same exclusions as _is_bounds_sketch: no leaf-intent default, never
    restored on navigation. Unlike a bounds sketch, a master is never left on
    by hand either — _rehide_bounds_sketches turns every master but the one
    being edited off whenever root is in play.
    """
    try:
        return (sk.name or '').startswith(_MASTER_SKETCH_PREFIX)
    except Exception:
        return False


def _body_key(comp_id, b):
    """One body on its own — see _sketch_key."""
    return leaf_key_for(b)


def leaf_key_for(entity):
    """The durable key for a sketch or body, given the entity (native or proxy).

    Public because the write paths — the Browser's rows, ⌘E — have an entity in hand
    and need the same identity the policy files it under.  Reads the names of the
    component's other sketches/bodies to find a repeat's '#n' (see _leaf_key)."""
    try:
        native = getattr(entity, 'nativeObject', None) or entity
    except Exception:
        native = entity
    try:
        ot = native.objectType or ''
    except Exception:
        return None
    if ot.endswith('Sketch'):
        kind = 'sk'
    elif ot.endswith('BRepBody'):
        kind = 'bd'
    else:
        return None
    try:
        comp = native.parentComponent
        if kind == 'bd' and body_uid(native):
            # Whether its id is its own or a copy's is decided in _keyed.
            for k, it in _keyed(_comp_id(comp), kind, comp.bRepBodies):
                if it == native:
                    return k
        name = native.name
        coll = comp.sketches if kind == 'sk' else comp.bRepBodies
        nth, found = 0, False
        for it in coll:
            try:
                if it.name != name:
                    continue
            except Exception:
                continue
            nth += 1
            if it == native:
                found = True
                break
        return _leaf_key(_comp_id(comp), kind, name, nth if found else 1)
    except Exception:
        return None


def _leaf_rec(data, lin, create=False):
    root = data.get(_LEAF_STORE)
    if not isinstance(root, dict):
        if not create:
            return None
        root = {}
        data[_LEAF_STORE] = root
    rec = root.get(lin)
    if not isinstance(rec, dict):
        if not create:
            return None
        rec = {}
        root[lin] = rec
    if not isinstance(rec.get('hidden'), list):
        rec['hidden'] = []
    if not isinstance(rec.get('mirror'), dict):
        rec['mirror'] = {}
    return rec


def load_hidden(design):
    """The leaf keys the user has hidden.  Read by the Browser so a row can be drawn
    as user-hidden from the record instead of from a dark bulb."""
    try:
        rec = _leaf_rec(_load(), _lineage(design))
        return set(rec['hidden']) if rec else set()
    except Exception:
        return set()


def load_intent(design):
    """(hidden leaf keys, isolate ledger's occurrence bulbs) from ONE read of the file —
    the Browser's per-build inputs for drawing a row as user-hidden from the record.
    An occurrence's intent is its ledger value when isolation holds its bulb, else the
    bulb itself: nothing else switches occurrence bulbs behind the user's back."""
    try:
        data = _load()
    except Exception:
        return set(), {}
    try:
        rec = _leaf_rec(data, _lineage(design))
        hidden = set(rec['hidden']) if rec else set()
    except Exception:
        hidden = set()
    occ_led, _fld = _ledger_read(data)
    return hidden, {k: bool(v) for k, v in occ_led.items()}


def note_occ_intent(occ, visible):
    """A deliberate hide/show of an occurrence's bulb. If isolation is holding that
    bulb, its ledger entry is what un-isolating writes back — so it takes the user's
    new choice, or the next jump would undo the gesture (and the Browser, which reads
    intent from the ledger, would draw the old one).

    The same goes for the saved record of the occurrence's level: restoring that
    level (Go Up, a jump, a sketch entry's un-isolate) writes the record back, so a
    hide or show made since it was taken has to land in it too — the record used to
    keep the old value, and the gesture was undone at the next navigation."""
    try:
        design = adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct)
        data = _load()
        occ_led, fld_led = _ledger_read(data)
        path = _occ_path_str(occ)
        visible = bool(visible)
        changed = False
        if path in occ_led and bool(occ_led[path]) != visible:
            occ_led[path] = visible
            data[_ISOLATE_LEDGER] = {'occ': occ_led, 'folders': fld_led}
            changed = True
        if design is not None:
            level = path.rpartition('/')[0]
            rec = _saved_level(data, design, level)
            node = rec.get(occ.name) if isinstance(rec, dict) else None
            if node is None or bool(node.get('v')) != visible:
                rec = dict(rec) if isinstance(rec, dict) else {}
                rec[occ.name] = dict(node or {}, v=visible)
                data.setdefault(_snap_key(design), {})[level] = rec
                changed = True
        if changed:
            _save(data)
            _log('NOTE_OCC_INTENT', path, 'shown' if visible else 'hidden')
    except Exception as _e:
        _log('NOTE_OCC_INTENT ERROR', _e)
    # A show under a hidden pass-through wrapper did nothing: the Browser never draws
    # the wrapper, so there is no bulb to click — light it too, and record that, or
    # the next restore of its level hides it again (Wings under Disregard, 2026-10-08).
    if visible:
        try:
            anc = occ.assemblyContext
        except Exception:
            anc = None
        while anc is not None:
            try:
                if is_disregarded(anc) and not anc.isLightBulbOn:
                    anc.isLightBulbOn = True
                    note_occ_intent(anc, True)
                anc = anc.assemblyContext
            except Exception:
                break


def set_leaf_intent(design, keys, hidden_flag):
    """Record that the user hid (or showed) these leaves.

    The ONLY way a gesture states intent — ⌘E, a Browser row's own bulb.  Policy
    writes (groups, isolation, the folder toggles) never come through here, which
    is the whole point: it is what keeps the two kinds of hide apart."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return
    try:
        data = _load()
        rec  = _leaf_rec(data, _lineage(design), create=True)
        hidden = set(rec['hidden'])
        for k in keys:
            if hidden_flag:
                hidden.add(k)
            else:
                hidden.discard(k)
        rec['hidden'] = sorted(hidden)
        _save(data)
        _log('SET_INTENT', 'hidden=' + str(bool(hidden_flag)), str(keys))
    except Exception as _e:
        _log('SET_INTENT ERROR', _e)


def carry_leaf_key(design, old_key, new_key):
    """A leaf was renamed: its key carries its name, so move the hidden record with it.

    Called by the Browser when the same token turns up under a different key — the
    only reliable rename signal there is, since a rename made in Fusion's own browser
    passes through nothing of ours."""
    carry_leaf_keys(design, [(old_key, new_key)])


def carry_leaf_keys(design, moves):
    """carry_leaf_key for several leaves AT ONCE: [(old key, new key)].

    A repeated name's '#n' is its place among the same-named (see _leaf_key), so
    deleting or renaming the first of two shifts the second onto the plain key —
    one leaf's old key is another's new one.  Carried one by one, the first move
    would overwrite a record the second still had to read; so every old record is
    read first, then all are written.  Each new key takes its leaf's old state
    whole — hidden or not, and the bulb mirror with it (else the shifted leaf's
    bulb reads as somebody else's change)."""
    moves = [(o, n) for o, n in (moves or ()) if o and n and o != n]
    if not moves:
        return
    try:
        data = _load()
        rec = _leaf_rec(data, _lineage(design))
        if not rec:
            return
        hidden, mirror = set(rec['hidden']), rec['mirror']
        before = (sorted(hidden), dict(mirror))
        was = [(n, o in hidden, mirror.get(o)) for o, n in moves]
        for o, _n in moves:
            hidden.discard(o)
            mirror.pop(o, None)
        for n, hid, mir in was:
            if hid:
                hidden.add(n)
            else:
                hidden.discard(n)
            if mir is None:
                mirror.pop(n, None)
            else:
                mirror[n] = mir
        if (sorted(hidden), mirror) == before:
            return
        rec['hidden'] = sorted(hidden)
        _save(data)
        _log('CARRY_KEY', moves)
    except Exception as _e:
        _log('CARRY_KEY ERROR', _e)


# ── Groups (the Browser's virtual folders) ────────────────────────────────────

_groups_cache = {'mtime': None, 'map': {}, 'pending': set()}


def hidden_group_members(lineage):
    """{leaf key: True} for every leaf inside a group the Browser has hidden.

    Read straight off folders.json — the Browser server owns that file and nothing
    here ever writes it — and cached on its mtime, so a policy pass costs one stat.
    Entries still keyed by the old entityToken are ignored; a leaf key always has
    its kind between two bars."""
    try:
        st = os.stat(_GROUPS_FILE).st_mtime
    except Exception:
        return {}
    if _groups_cache['mtime'] != st:
        out = {}
        pending = set()
        try:
            with open(_GROUPS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for lin, d in ((raw or {}).get('byDoc') or {}).items():
                folders = {}
                for fdef in (d.get('folders') or []):
                    if fdef and fdef.get('id'):
                        folders[fdef['id']] = fdef
                eff = {}

                def _is_hidden(fid, _seen=None):
                    """A group is hidden if it, or any group above it, is hidden."""
                    if fid in eff:
                        return eff[fid]
                    f = folders.get(fid)
                    if f is None:
                        return False
                    _seen = _seen or set()
                    if fid in _seen:
                        return False              # cycle — treat as not hidden
                    _seen.add(fid)
                    val = bool(f.get('hidden')) or _is_hidden(f.get('parent') or '', _seen)
                    eff[fid] = val
                    return val

                members = {}
                for key, fid in ((d.get('leaves') or {}).items()):
                    if not isinstance(key, str) or key.count('|') < 2:
                        continue              # legacy entityToken, not a leaf key
                    if _is_hidden(fid):
                        members[key] = True
                out[lin] = members
                # The window sets `keyed` once it has rewritten this document's
                # membership to durable keys.  Until then the map above is empty or
                # partial and must not be trusted.  A document with no membership at
                # all has nothing to wait for.
                if (d.get('leaves') or {}) and not d.get('keyed'):
                    pending.add(lin)
        except Exception as _e:
            _log('READ_GROUPS ERROR', _e)
            out, pending = {}, set()
        _groups_cache['mtime']   = st
        _groups_cache['map']     = out
        _groups_cache['pending'] = pending
    return _groups_cache['map'].get(lineage) or {}


def groups_pending(lineage):
    """True while this document's group overlay is still keyed by entityToken.

    The leaf pass must not run then: the groups would read as EMPTY, every member of
    a hidden group would look like a bulb the user turned off, and the first pass
    would seed the intent record with exactly the mistake this rewrite removes.  The
    Browser window rewrites the keys the first time it draws the document (it is the
    only place that holds both the token and the key), so this is at most a wait, and
    leaving the bulbs untouched meanwhile is the safe thing to do."""
    hidden_group_members(lineage)             # refresh the cache if the file moved
    return lineage in (_groups_cache.get('pending') or set())



# ── The leaf pass ─────────────────────────────────────────────────────────────

def _all_leaves(design):
    """(key, entity) for every sketch and body in the design."""
    out = []
    try:
        for comp in design.allComponents:
            cid = _comp_id(comp)
            for k, sk in _keyed(cid, 'sk', comp.sketches):
                if _is_bounds_sketch(sk) or _is_master_sketch(sk):
                    continue
                out.append((k, sk))
            for k, b in _keyed(cid, 'bd', comp.bRepBodies):
                out.append((k, b))
    except Exception as _e:
        _log('ALL_LEAVES ERROR', _e)
    return out


def _adopt_external(leaves, hidden, mirror, groups, skip_keys):
    """Fold changes made OUTSIDE this code into intent — and only those.

    A bulb equal to the mirror is one we wrote: policy, not intent, and it says
    nothing.  A bulb that differs was written by something else, and that is a real
    statement about what the user wants to see, whether it came from Fusion's own
    browser, the Hide command or an undo.

    A leaf never seen before is SEEDED rather than adopted — except one that is off
    with no group to explain it, which must have been hidden by hand before this
    record existed.

    Returns what each bulb was found at, so the write pass below doesn't read all
    of them a second time."""
    was = {}
    for key, ent in leaves:
        if key in skip_keys:
            continue
        try:
            cur = bool(ent.isLightBulbOn)
        except Exception:
            continue
        was[key] = cur
        prev = mirror.get(key)
        if prev is None:
            mirror[key] = cur
            if not cur and not groups.get(key):
                hidden.add(key)
            continue
        if cur == prev:
            continue
        mirror[key] = cur
        if cur:
            hidden.discard(key)
        else:
            hidden.add(key)
        _log('ADOPT', key, 'bulb=' + str(cur))
    return was


def _apply_leaf_bulbs(leaves, hidden, mirror, groups, skip_keys, was):
    """Write every leaf bulb to what intent and the groups say it should be.

    Only ever writes on a real change: every light-bulb write Fusion accepts costs
    an undo transaction it keeps for the life of the document.

    The mirror is stamped whether or not the bulb had to move — it records what this
    code ASSERTS the value is, and asserting a value it already holds is still our
    claim on it."""
    for key, ent in leaves:
        if key in skip_keys:
            continue
        want = (key not in hidden) and (not groups.get(key))
        cur = was.get(key)
        if cur is None:
            try:
                cur = bool(ent.isLightBulbOn)
            except Exception:
                continue
        if cur != want:
            try:
                ent.isLightBulbOn = want
            except Exception:
                continue
        mirror[key] = want


def _resync_mirror(comp, mirror):
    """Stamp the mirror with one component's current sketch bulbs.

    The sketch-edit path writes those bulbs itself (the edited sketch on, the others
    per the toggle), and every write this code makes has to be mirrored or the next
    pass would read it back as somebody else's."""
    try:
        cid = _comp_id(comp)
        for k, sk in _keyed(cid, 'sk', comp.sketches):
            if _is_bounds_sketch(sk) or _is_master_sketch(sk):
                continue          # excluded from the leaf-intent system entirely
            if k:
                try:
                    mirror[k] = bool(sk.isLightBulbOn)
                except Exception:
                    pass
    except Exception:
        pass


# ── Migration off the old per-version, guessed-from-bulbs record ──────────────

_OLD_HIDDEN_KEY = '__sketch_hidden__'


def _migrate_leaf_store(design, data, groups):
    """Build the lineage record the first time a document is seen.

    The old record is per version and, because it was inferred from bulbs, holds
    every group hide as though it were a deliberate one.  So it is carried over
    MINUS anything a hidden group already explains — that subtraction is the
    one-time cleanup of the bug this whole section exists to remove.  Bodies never
    had a record at all; theirs is seeded the same way, from their current bulbs.
    """
    rec = _leaf_rec(data, _lineage(design), create=True)
    if rec['mirror'] or rec['hidden']:
        return rec                              # already migrated
    old = set()
    try:
        raw = data.get(_doc(design), {}).get(_OLD_HIDDEN_KEY)
        if isinstance(raw, list):
            for k in raw:
                # '<comp id>|<name>' -> '<comp id>|sk|<name>'
                cid, _, name = str(k).partition('|')
                if name:
                    old.add(_leaf_key(cid, 'sk', name))
    except Exception:
        pass
    hidden, mirror = set(), {}
    dropped = 0
    for key, ent in _all_leaves(design):
        try:
            cur = bool(ent.isLightBulbOn)
        except Exception:
            continue
        mirror[key] = cur
        if groups.get(key):
            if key in old:
                dropped += 1                    # a group hide misfiled as a user hide
            continue
        if not cur:
            hidden.add(key)
    rec['hidden'] = sorted(hidden)
    rec['mirror'] = mirror
    _log('MIGRATE_LEAF_STORE', _lineage(design),
         'old=' + str(len(old)), 'kept=' + str(len(hidden)),
         'group_hides_dropped=' + str(dropped), 'mirror=' + str(len(mirror)))
    _save(data)          # a once-per-document event; the caller only saves on change
    return rec


def _focus_has_bodies(design):
    """Would bodies show on arrival in the focus — its own lit subtree's, or else the
    level-up fallback's (the parent's, drawn dimmed)?  Arrival is always isolated.
    Only when neither has a body does a component arrive with its sketches on, so
    the view is never empty; otherwise arrival is bodies only, and the cycle brings
    the sketches on."""
    try:
        return _kind_would_show(design, 'body', isolated=True)
    except Exception:
        return False


def _resolve_transient(design, active_comp, active_id, edit_token, ctx_id=None):
    """Read (sketch_shown, body_shown) for the current activation context, resetting
    each to its default when the relevant context changes.

    Two independent context keys, because bodies and sketches have different
    lifetimes:
      • sketches + isolation reset on "<comp_id>|<sketch_token>" — entering or
        leaving a sketch is a context change for them (the sketch-edit default is
        OFF so only the edited sketch shows, and the other sketches come back on a
        different way when you leave), so the toggle only lasts within one sketch /
        one component.
      • bodies reset on "<comp_id>" ALONE, so the body toggle survives anything
        short of switching component.  The sketch enter/exit brackets override
        that explicitly — bodies are forced OFF on enter and back ON on exit (see
        § Body visibility across sketch edits), so the toggle never outlives the
        edit it was made in.

    ctx_id overrides the component the contexts are keyed on — a master session
    keys them on the original sketch's component, since Fusion makes the ROOT active
    while the master is open but the view is still centred where you came from.
    """
    ctx_id   = active_id if ctx_id is None else ctx_id
    ctx_key  = '{}|{}'.format(ctx_id, edit_token)
    body_ctx = str(ctx_id)
    state    = _read_shared_state()
    # Bodies of the ACTIVE component default ON (you want to see what you're in).
    # Its sketches default OFF wherever bodies would show — its own, or the level-up
    # fallback's (bodies only — the first step of the cycle; Cycle Visibility brings
    # them on) — and ON only where neither has any (otherwise the view would be empty).
    # While editing a sketch they default OFF so only the sketch you are editing
    # shows and the other sketches are hidden until you toggle them on.
    sketch_default = not edit_token
    body_default   = True
    dirty = False
    entered_sketch = False

    # ── bodies: reset only when the active COMPONENT changes ──────────────────
    if state.get('body_ctx') != body_ctx:
        state['body_ctx']       = body_ctx
        state['body_shown']     = body_default
        state['bodies_visible'] = body_default   # legacy Browser-sync mirror
        dirty = True

    # ── sketches + isolation: reset when component OR edited sketch changes ───
    if state.get('policy_ctx') != ctx_key:
        if sketch_default and _focus_has_bodies(design):
            sketch_default = False
        state['policy_ctx']       = ctx_key
        state['sketch_shown']     = sketch_default
        # Isolation is the default the moment the context changes — the Toggle
        # Isolation un-isolate is transient, exactly like the sketch toggle, so it
        # is reset here.  This is a LOGICAL reset only: the physical re-isolation
        # onto the new active occurrence is already done by whoever changed the
        # activation (Browser / nav add-ins).  Entering a sketch, it is OFF instead
        # (enter_sketch_body_default has un-isolated, and says so).
        state['isolate_off']      = bool(edit_token) and \
            bool(state.pop(ENTER_UNISOLATED_KEY, False))
        state['sketches_visible'] = sketch_default   # legacy Browser-sync mirror
        dirty = True
        entered_sketch = bool(edit_token)

    # ── a NEW sketch continues the cycle it was started from ──────────────────
    # Overrides both entry defaults above (and enter_sketch_body_default, which ran
    # before this) — see § New sketch keeps the cycle position.
    if entered_sketch and NEW_SKETCH_CARRY_KEY in state:
        carry = state.pop(NEW_SKETCH_CARRY_KEY) or {}
        dirty = True
        if _time.time() - float(carry.get('at', 0)) < _NEW_SKETCH_CARRY_TTL:
            for key, val in (('sketch_shown', carry.get('sketch')),
                             ('body_shown',   carry.get('body'))):
                val = bool(val)
                state[key] = val
                state[_MIRROR_KEY[key]] = val
            _log('NEW_SKETCH_CARRY', 'sketch_shown=' + str(state['sketch_shown']),
                 'body_shown=' + str(state['body_shown']))

    if dirty:
        _write_shared_state(state)
    return (bool(state.get('sketch_shown', sketch_default)),
            bool(state.get('body_shown', body_default)))


def _apply_sketch_edit_visibility(comp, edit_native, show_others, hidden=None,
                                  keep=None, aids=True):
    """While a sketch is being edited, keep the edited sketch visible no matter
    what; the Sketch toggle (show_others) only governs the OTHER sketches in the
    active component.  The folder bulb is forced on so the edited sketch can show;
    per-sketch bulbs do the gating.

    The edited sketch is matched by object identity, entity token AND name (any
    one is enough) so a proxy/native or token mismatch can't strand it.  If we
    were handed no edited sketch at all (edit_native is None — identity unknown),
    every sketch is kept visible: hiding the one you're editing is the worst
    possible outcome, so we never risk it.

    A sketch the user deliberately hid (hidden) stays hidden alongside the
    show_others gate — unless it is the one being edited, which always shows.

    keep: one more sketch that always shows, without annotations — a master
    session's original sketch (see that section). aids=False shows no annotations
    at all: used for the original sketch's own component, where edit_native is the
    original sketch and is NOT the one being edited."""
    _set_folder(comp, 'isSketchFolderLightBulbOn', True)
    hidden  = hidden or set()
    comp_id = _comp_id(comp)
    edit_tok = edit_name = None
    try:
        if edit_native is not None:
            edit_tok  = edit_native.entityToken
            edit_name = edit_native.name
    except Exception:
        pass
    ref_sk = _open_ref_sketch(edit_native) if aids else None
    if edit_native is None:
        show_others = True  # unknown which sketch is being edited → keep all on
        # ...and that has to beat the recorded hides too. Every sketch here is a
        # candidate for the one being edited, and the recorded hide now OUTLIVES the
        # edit (it is what puts a sketch you opened from hidden back to hidden on the
        # way out) — so consulting it on this path switched off the very sketch that
        # was open, which is the one thing this function must never do.
        hidden = set()
    try:
        for sk_key, sk in _keyed(comp_id, 'sk', comp.sketches):
            try:
                is_edited = False
                if edit_native is not None:
                    try:
                        is_edited = (sk == edit_native)
                    except Exception:
                        is_edited = False
                    if not is_edited and edit_tok:
                        try:
                            is_edited = (sk.entityToken == edit_tok)
                        except Exception:
                            pass
                    if not is_edited and edit_name:
                        is_edited = (sk.name == edit_name)
                is_kept = (keep is not None and _same_sketch(sk, keep)) or \
                    (ref_sk is not None and sk == ref_sk)
                if is_edited or is_kept or not (_is_bounds_sketch(sk) or _is_master_sketch(sk)):
                    _set_bulb(sk, True if (is_edited or is_kept) else
                              (show_others and sk_key not in hidden))
                # Annotations show only for the sketch actually being edited.
                _set_sketch_aids(sk, bool(is_edited) and aids)
            except Exception:
                pass
    except Exception:
        pass


def _rehide_bounds_sketches(design, ctx_key, active_comp, active_id):
    """Turn the relevant Bounds sketches' bulbs back off the moment the active
    context (component + edited sketch) changes.

    Bounds sketches sit outside the leaf-intent system (see _is_bounds_sketch)
    precisely so nothing here ever infers a deliberate "show" from them and
    nothing ever restores them the way an ordinary sketch is restored on exit.
    That leaves manually turning one's bulb on (in Fusion's own browser) as the
    only way one ever shows — and this is what makes that show last only for
    the context it happened in: the very next navigation, into or out of
    anything, wipes it.  Within the same context nothing here touches the bulb
    at all, so a sketch shown by hand stays shown until you actually move on.

    Scoped to just the component being LEFT and the one being ENTERED — never
    the whole document.  This runs on every navigation, including the one that
    opens a sketch for edit, and a full design.allComponents sweep right at
    that moment is both pointless (every other component's bounds sketches
    are already off, or hidden by isolation regardless) and something to keep
    off the critical path of entering an edit.
    """
    try:
        state = _read_shared_state()
        if state.get('bounds_ctx') == ctx_key:
            return                          # same context — leave bulbs alone
        prev_id = state.get('bounds_prev_comp')
        state['bounds_ctx'] = ctx_key
        state['bounds_prev_comp'] = active_id
        _write_shared_state(state)

        targets = [active_comp]
        if prev_id and prev_id != active_id:
            try:
                prev_comp = design.allComponents.itemById(prev_id)
            except Exception:
                prev_comp = None
            if prev_comp is not None:
                targets.append(prev_comp)
        # The open sketch's reference sketch stays (see _open_ref_sketch).
        try:
            editing = active_edit_sketch(design)
            ref_sk = _open_ref_sketch((editing.nativeObject or editing) if editing else None)
        except Exception:
            ref_sk = None
        for comp in targets:
            for sk in comp.sketches:
                if _is_bounds_sketch(sk) and not (ref_sk is not None and sk == ref_sk):
                    _set_bulb(sk, False)

        # Masters only ever live in the root and are always hidden (the
        # browser window's Masters switch only shows their folder row) — so
        # re-assert that here whenever root is in play.
        # The sketch actually being edited is handled by
        # _apply_sketch_edit_visibility, not here; skip it so this pass never
        # fights that.
        if design.rootComponent in targets:
            # active_edit_sketch, not design.activeEditObject directly — the
            # latter raises InternalValidationError at some depths (see
            # is_editing_sketch's docstring); a master is never nested, but
            # this file already has the depth-safe way to ask, so use it.
            try:
                editing = active_edit_sketch(design)
                edit_native = (editing.nativeObject or editing) if editing else None
            except Exception:
                edit_native = None
            for sk in design.rootComponent.sketches:
                if _is_master_sketch(sk) and sk != edit_native:
                    _set_bulb(sk, False)
    except Exception as _e:
        _log('REHIDE_BOUNDS ERROR', _e, _traceback.format_exc())


def apply_policy(design, force_edit_sketch=None):
    """One undo step around _apply_policy (below) — it and the dimming it ends with
    were ~40 separate steps per jump, and every step makes the next write slower."""
    with one_step('Visibility'):
        return _apply_policy(design, force_edit_sketch)


def _apply_policy(design, force_edit_sketch=None):
    """Recompute body/sketch/origin folder visibility for every component.

    Idempotent and authoritative — safe to call after any activation, restore,
    isolate, or creation.  Runs in a single event batch so only one frame renders
    (no flashing).

    Transient, leaf-aware model:
      • Isolation is always on, so the active subtree shows and strict ancestors
        of the active component are hidden.
      • DEFAULTS — sketches AND bodies show (neither while editing a sketch).
        These defaults are recomputed (reset) every
        time the active component (or the edited sketch) changes, so the toggles
        below are only ever temporary.
      • bodies   — body_shown gates the body folder of the WHOLE active subtree
                   (active component + every component nested under it); Toggle
                   Body Visibility flips it transiently.  Strict ancestors stay
                   hidden; everything outside the subtree is hidden by isolation.
      • sketches — sketch_shown gates the ACTIVE component's own sketch folder
                   only; Toggle Sketch Visibility flips it transiently.  Every
                   other component's sketch folder is hidden (children included)
                   unless isolation is off.
                   While editing a sketch the edited sketch always shows and
                   sketch_shown gates the other sketches.
      • fallback — bodies on with nothing to show in the focus subtree gate the
                   PARENT's subtree instead, and isolation widens onto the parent to
                   light the siblings (their sketches stay off). Sketches never
                   fall back.
      • origins  — always hidden at the folder level.  The ONLY time origin
                   geometry shows is the two in-plane axes during a sketch edit,
                   which the sketch-edit handler drives directly; apply_policy
                   leaves the origin folder alone while a sketch is open.
    """
    try:
        active_comp = design.activeComponent
        active_id   = _comp_id(active_comp)
        active_occ  = _active_occ(design)

        if force_edit_sketch is not None:
            # The SketchActivate handler KNOWS a sketch edit just began and which
            # sketch it is (identified from the timeline marker), so it tells us
            # directly.  This is immune to the brief window right after enter
            # where SketchStop.isEnabled / activeEditObject have not yet settled —
            # which is what left the other sketches showing on enter.
            editing_sketch = True
            edit_native = force_edit_sketch.nativeObject or force_edit_sketch
            try:
                edit_token = edit_native.entityToken
            except Exception:
                edit_token = ''
        else:
            editing_sketch, edit_native, edit_token = _edit_sketch_info(design)

        # ── Leaf visibility inputs ────────────────────────────────────────
        # Intent, the groups and the mirror are read once; every sketch and body
        # bulb below is then a pure function of them (see the leaf section above).
        # Nothing here reads a bulb to decide what the user wants — the only bulb
        # read is _adopt_external's, comparing against what we last wrote.
        #
        # While a sketch is open every sketch in THAT component is edit-transient:
        # Fusion shows the one you opened and _apply_sketch_edit_visibility owns
        # the rest, so those keys sit out both the adoption and the write, and are
        # stamped into the mirror afterwards instead.
        _lin     = _lineage(design)
        _pending = groups_pending(_lin)
        _data  = _load()
        groups = hidden_group_members(_lin)
        _rec   = None if _pending else _migrate_leaf_store(design, _data, groups)
        hidden = set(_rec['hidden']) if _rec else set()
        mirror = dict(_rec['mirror']) if _rec else {}
        leaves = [] if _pending else _all_leaves(design)

        # ── Master session: the FOCUS is the original sketch's occurrence ─────
        # Opening a master (a root sketch) makes Fusion activate the root, which
        # would put the whole model in the "active subtree". While a session's
        # master is open the view instead stays centred on the sketch you came from:
        # isolation, ancestors, the subtree the toggles gate and the transient
        # contexts all key on its occurrence, and that sketch always shows. Outside
        # a session focus == active and everything below is exactly as before.
        companion = None
        if editing_sketch and edit_native is not None:
            companion = master_session_editing(design, edit_native)
        comp_sketch = companion[0] if companion else None
        if companion:
            focus_occ  = companion[1]
            focus_comp = focus_occ.component if focus_occ else design.rootComponent
            comp_cid   = _comp_id(comp_sketch.parentComponent)
        else:
            focus_occ, focus_comp, comp_cid = active_occ, active_comp, None
        focus_id = _comp_id(focus_comp)

        # ── Suspend-mode peek: the peek is the focus ──────────────────────────
        # (see § Suspend mode: peek) — and the toggles' context: arriving at a peek
        # resets them to its arrival defaults, judged on the peek (no sketch-edit
        # default — the peek isn't the sketch), and Cycle Visibility acts on it. The
        # real focus's subtree keeps the values parked in peek_home (home_* below).
        ctx_id = focus_id
        ctx_edit = edit_token
        peeking, peek_occ = _peek_target(design)
        real_focus = focus_occ
        if peeking:
            focus_occ  = peek_occ
            focus_comp = peek_occ.component if peek_occ else design.rootComponent
            focus_id   = _comp_id(focus_comp)
            ctx_id, ctx_edit = focus_id, ''

        edit_skip = set()
        if editing_sketch:
            try:
                for _k, _sk in _keyed(active_id, 'sk', active_comp.sketches):
                    edit_skip.add(_k)
                if companion and comp_cid != active_id:
                    for _k, _sk in _keyed(comp_cid, 'sk',
                                          comp_sketch.parentComponent.sketches):
                        edit_skip.add(_k)
            except Exception:
                pass
        _was = _adopt_external(leaves, hidden, mirror, groups, edit_skip)

        # When the user has temporarily turned isolation OFF (Toggle Isolation),
        # the body/sketch toggles act on EVERY visible component's folder, not
        # just the active subtree.  Read AFTER _resolve_transient: entering a sketch
        # resets the flag to un-isolated there, and a stale read made the first pass
        # widen isolation onto the parent while the flag already said off.
        sketch_shown, body_shown = _resolve_transient(
            design, active_comp, active_id, ctx_edit, ctx_id=ctx_id)
        isolate_off = isolate_off_active()
        # What the active component (and the real focus's subtree) draws with: its own
        # toggles — parked while a peek holds the live ones.
        home_sketch, home_body = sketch_shown, body_shown
        if peeking:
            home_sketch, home_body = _home_toggles(_read_shared_state())

        # ── Body fallback one level up ──────────────────────────────────────
        # Bodies ON with nothing to show in the focus subtree act on the parent's
        # level instead (see isolate_parent_occ's section). Decided from intent +
        # groups, like the bulbs themselves, and from which child occurrences are
        # lit. Not while un-isolated — everything shows then. Peeking, the same as
        # anywhere else, with the active path kept lit through the widening.
        body_up = False
        if focus_occ is not None and not isolate_off and body_shown:
            body_up = not _has_visible_leaf(_visible_subtree_comps(focus_occ),
                                            'bd', hidden, groups)
        focus_path = _occ_path_str(focus_occ) if focus_occ is not None else ''
        _st = _read_shared_state()
        widened_for = _st.get('widened_for') or ''
        if body_up:
            if widened_for != focus_path:
                _widen_to_parent(design, focus_occ,
                                 keep=real_focus if peeking else None)
        elif widened_for:
            if widened_for == focus_path and not isolate_off:
                isolate_occurrence_no_snapshot(           # clears it
                    design, focus_occ, keep=real_focus if peeking else None)
            else:
                _clear_widened()   # whoever moved the focus already re-isolated
        _st = _read_shared_state()
        if bool(_st.get('body_up')) != body_up:
            _st['body_up'] = body_up
            _write_shared_state(_st)

        # What each toggle gates. Bodies: the WHOLE focus subtree (the focus plus
        # every component nested under it), or the parent's whole subtree while
        # they fall back. Sketches: the focus component's OWN sketches only, never
        # its children's or anyone else's. Only un-isolating shows more.
        #
        # Strict ancestors of the lit scope — their container bodies would otherwise
        # float around the isolated subtree, so their body folder is hidden.
        # Siblings/unrelated components are hidden by the isolate occurrence-bulbs.
        focus_ids = {focus_id} | _descendant_ids(focus_comp)
        body_ids  = focus_ids
        scope_occ = focus_occ
        if body_up:
            scope_occ  = isolate_parent_occ(focus_occ)
            scope_comp = scope_occ.component if scope_occ else design.rootComponent
            body_ids   = focus_ids | {_comp_id(scope_comp)} | _descendant_ids(scope_comp)
        sketch_ids = {focus_id}
        ancestors  = _ancestor_ids(scope_occ)
        # Peeking, the real focus's subtree keeps its parked toggles (they win over the
        # peek's where the two overlap), and its ancestors — lit by the peek's
        # keep path — hide their container bodies like the peek's own ancestors do.
        real_ids = set()
        if peeking:
            rcomp = real_focus.component if real_focus is not None else design.rootComponent
            real_ids = {_comp_id(rcomp)} | _descendant_ids(rcomp)
            ancestors |= _ancestor_ids(real_focus)

        # Section Sketch's slice, while it is on in the open sketch, DRAWS the bodies
        # (cut at the plane, as custom graphics), so their folders stay off and what
        # they would have been is handed over instead — see _slice_hook. Handed over
        # in every sketch, slice or not: its references follow the bodies on screen.
        slice_hook = _slice_hook() if editing_sketch else None
        slice_owns = _slice_owns(slice_hook)
        slice_would = {}

        for comp in design.allComponents:
            cid       = _comp_id(comp)
            is_active = (cid == active_id)

            # ── bodies ──────────────────────────────────────────────────────
            # Un-isolated, every component is visible, so the toggles act on ALL of
            # them — gate the whole design.
            if is_active or cid in real_ids:
                body_on = home_body
            elif isolate_off or cid in body_ids:
                body_on = body_shown
            else:
                body_on = cid not in ancestors
            if slice_hook is not None:
                slice_would[cid] = body_on
                if slice_owns:
                    body_on = False
            _set_folder(comp, 'isBodiesFolderLightBulbOn', body_on)

            # ── sketches ────────────────────────────────────────────────────
            # Active + editing is handled after the loop (per-sketch bulbs); the
            # rest of the scope follows sketch_shown.
            if not ((is_active and editing_sketch) or (companion and cid == comp_cid)):
                if is_active:
                    sk_on = home_sketch
                elif isolate_off or cid in sketch_ids:
                    sk_on = sketch_shown
                else:
                    sk_on = False
                _set_folder(comp, 'isSketchFolderLightBulbOn', sk_on)
                # Per-sketch bulbs are ONLY ever switched off transiently, to hide
                # the OTHER sketches while one is being edited.  For every
                # component that is not the one currently being edited the folder
                # is the sole authority for which sketches the policy shows, so
                # make sure no individual sketch is left stranded-off underneath
                # it — anything the user did NOT deliberately hide goes back on.
                # This self-heals the state left
                # behind when a sketch edit is exited by navigating away (Go Up /
                # activating another component) instead of via Finish Sketch —
                # apply_policy runs on every navigation, so the stranded bulbs are
                # cleaned up the next time round no matter how you left the sketch.
                for sk in comp.sketches:
                    _set_sketch_aids(sk, False)   # no annotations outside sketch edit

            # ── origins (hidden by default, except live sketch-edit axis reveal, or
            # a component the browser's Origin group turned an entity on in — see
            # origin_folder_wanted) ──────────────────────────────────────────
            if not editing_sketch:
                _set_folder(comp, 'isOriginFolderLightBulbOn',
                            origin_folder_wanted(cid))

        # The single write of every sketch and body bulb in the document.
        _apply_leaf_bulbs(leaves, hidden, mirror, groups, edit_skip, _was)

        if editing_sketch:
            _log('SKETCH_EDIT apply',
                 'comp=' + active_comp.name,
                 'edit_native=' + (edit_native.name if edit_native else 'None'),
                 'sketch_shown=' + str(home_sketch))
            # A hidden group counts alongside a deliberate hide here: neither shows
            # while its neighbours do, and the sketch being edited beats both.
            same_comp = companion is not None and comp_cid == active_id
            _apply_sketch_edit_visibility(active_comp, edit_native, home_sketch,
                                          hidden | set(groups),
                                          keep=comp_sketch if same_comp else None)
            _resync_mirror(active_comp, mirror)
            # The original sketch of a master session, in its own component: shown
            # like an edited sketch (the Sketch toggle gates its neighbours) but
            # without annotations — it is not the one being edited.
            if companion is not None and not same_comp:
                cc = comp_sketch.parentComponent
                _apply_sketch_edit_visibility(cc, comp_sketch, home_sketch,
                                              hidden | set(groups), aids=False)
                _resync_mirror(cc, mirror)
                _log('SKETCH_EDIT master session', 'focus=' + focus_comp.name,
                     'original=' + comp_sketch.name)

        # Last, and scoped to just the component being left + the one entered —
        # see _rehide_bounds_sketches. Kept off the critical path above so it can
        # never be the thing standing between a sketch you just opened and it
        # actually showing.
        _rehide_bounds_sketches(design, '{}|{}'.format(active_id, edit_token),
                                 active_comp, active_id)

        # apply_policy runs on every navigation, and the snapshot file holds every
        # document's records — so it is only rewritten when something actually moved.
        if _rec is not None:
            new_hidden = sorted(hidden)
            if new_hidden != _rec['hidden'] or mirror != _rec['mirror']:
                _rec['hidden'] = new_hidden
                _rec['mirror'] = mirror
                _save(_data)

        _dim_now(editing_sketch)
        if slice_hook is not None:
            try:
                slice_hook.policy(slice_would)
            except Exception as _e:
                _log('SLICE_HOOK ERROR', _e)
    except Exception as _e:
        _log('APPLY_POLICY ERROR', _e, _traceback.format_exc())


# Dim Outside Active re-evaluates after commands end, but the Browser vetoes its own
# nav commands at commandStarting (and a mouse activation runs no command at all), so
# no commandTerminated follows them. Every path that moves the focus or changes body
# visibility ends in apply_policy, so Dim runs here — synchronously, so the dimming
# lands in the same frame as the bulbs above instead of a beat later. Our sketch-edit
# verdict is passed along: activeEditObject hasn't settled right after a sketch opens.
_DIM_HOOK = 'dim_outside_active_hook'   # registered by Dim Outside Active's run()


def _dim_now(editing_sketch):
    hook = sys.modules.get(_DIM_HOOK)
    if hook is not None:
        try:
            hook.apply(bool(editing_sketch))
        except Exception:
            pass


# Section Sketch's slice in a sketch edit. A Section Analysis can only cut live bodies
# with an outline round every cut and in component colours, and it clips custom
# graphics as well, so nothing can be drawn over it. So while the slice is on, the
# bodies' folders stay off and the slice draws them itself: `owns_bodies()` asks
# whether it is on, `policy({comp id: body folder would be on})` hands over which —
# in every sketch, slice on or off, since Section Sketch's references follow it too.
_SLICE_HOOK = 'section_slice_hook'      # registered by Section Sketch's run()


def _slice_hook():
    return sys.modules.get(_SLICE_HOOK)


def _slice_owns(hook):
    try:
        return hook is not None and bool(hook.owns_bodies())
    except Exception:
        return False


_MIRROR_KEY = {'sketch_shown': 'sketches_visible', 'body_shown': 'bodies_visible'}
_CMD_ID     = {'sketch_shown': 'ToggleSketchVisibilityCmd',
               'body_shown':   'ToggleBodyVisibilityCmd'}


def _flag_default(design, key):
    """body default is always ON; sketch default is OFF while editing a sketch
    (only the edited sketch shows), else depth-based."""
    if key == 'body_shown':
        return True
    return not _toggles_in_sketch(design)


def _toggles_in_sketch(design):
    """Do the toggles follow the sketch-edit rules? Not while a suspend-mode peek
    holds them: the peek is not the sketch, so they act as they would outside one."""
    return is_editing_sketch(design) and not is_peeking(design)


def _sync_cmd_checkbox(key, value):
    """Reflect a flag's new value on its toolbar checkbox.  Used to keep the
    OTHER toggle's checkbox in step when the mutual-exclusion rule forces it on."""
    try:
        ui = adsk.core.Application.get().userInterface
        cmd_def = ui.commandDefinitions.itemById(_CMD_ID[key])
        if cmd_def:
            cmd_def.isChecked = bool(value)
    except Exception:
        pass


def _toggle_flag(design, key):
    """Flip a transient visibility flag (sketch_shown / body_shown) for the
    current activation context and re-apply the policy.  Returns the new value.

    Mutual-exclusion rule: bodies and sketches can never both be off at once.
    If this toggle turns one off while the other is already off, the other is
    forced back on (and its checkbox synced)."""
    try:
        apply_policy(design)  # make sure the flag reflects the current context
        state   = _read_shared_state()
        new_val = not bool(state.get(key, _flag_default(design, key)))
        state[key] = new_val
        # keep the legacy mirror used by the Browser sync in step
        state[_MIRROR_KEY[key]] = new_val
        _home_follow(state, key, new_val)

        # ── mutual exclusion: don't allow both flags off simultaneously ──
        # ...EXCEPT while editing a sketch.  The sketch being edited is always
        # visible, so bodies-off + sketches-off still shows something — there is
        # no "empty screen" to guard against.  Turning bodies off inside a sketch
        # must therefore NOT force the other sketches back on.
        if not new_val and not _toggles_in_sketch(design):
            other = 'body_shown' if key == 'sketch_shown' else 'sketch_shown'
            if not bool(state.get(other, _flag_default(design, other))):
                state[other] = True
                state[_MIRROR_KEY[other]] = True
                _home_follow(state, other, True)
                _sync_cmd_checkbox(other, True)

        _write_shared_state(state)
        apply_policy(design)
        return new_val
    except Exception as _e:
        _log('TOGGLE_FLAG ERROR', key, _e, _traceback.format_exc())
        return False


def toggle_sketches(design):
    """Temporarily show/hide the active component's own sketches (the edited
    sketch is always kept visible during a sketch edit)."""
    return _toggle_flag(design, 'sketch_shown')


def toggle_bodies(design):
    """Temporarily show/hide the active component's own bodies."""
    return _toggle_flag(design, 'body_shown')


def _invert_step(step):
    """The same cycle walked backwards ({a: b} → {b: a})."""
    return {v: k for k, v in step.items()}


def cycle_visibility(design, reverse=False):
    """Step (sketch_shown, body_shown) for the current activation context and
    re-apply the policy (reverse=True walks each cycle backwards; the off-cycle
    entries — both off outside a sketch, both on inside one — land where they do
    going forwards):
         bodies only → both on → sketches only → bodies only
    and inside a sketch edit, where neither is the default (the edited sketch always
    shows, so there is no empty screen to guard against):
         neither → bodies only → sketches only → neither
    (just neither ↔ bodies only when the open sketch has no neighbours for the
    Sketch toggle to gate — see _edit_sketch_toggle_matters).  Outside a sketch, bodies
    only is where arriving in a component with bodies starts (_resolve_transient),
    both off (off the cycle) steps to bodies only, and any step that
    would show nothing at all is skipped (see _kind_would_show).  Bodies with nothing
    to show fall back one level up (apply_policy); sketches never do.
    Returns the new (sketch_shown, body_shown)."""
    try:
        apply_policy(design)  # make sure the flags reflect the current context
        state  = _read_shared_state()
        sketch = bool(state.get('sketch_shown', _flag_default(design, 'sketch_shown')))
        body   = bool(state.get('body_shown',   _flag_default(design, 'body_shown')))
        if _toggles_in_sketch(design):
            if _edit_sketch_toggle_matters(design):
                # no both-on step inside a sketch; a carried-over both on (Sketch:
                # New keeps the cycle position) steps back to neither
                step = {(False, False): (False, True),
                        (False, True):  (True, False),
                        (True, False):  (False, False)}
                if reverse:
                    step = _invert_step(step)
                new = step.get((sketch, body), (False, False))
            else:
                # no neighbour sketches: the sketch steps would look identical to
                # their neighbours, so skip them — neither ↔ bodies only
                new = (False, not body)
        else:
            # Skip any step that would leave the screen empty (e.g. sketches only
            # where neither this component nor the level-up fallback has a sketch).
            step = {(False, True): (True, True),
                    (True, True):  (True, False),
                    (True, False): (False, True)}
            if reverse:
                step = _invert_step(step)
            has = {'sketch': _kind_would_show(design, 'sketch'),
                   'body':   _kind_would_show(design, 'body')}
            new = step.get((sketch, body), (False, True))
            for _ in range(2):
                if (new[0] and has['sketch']) or (new[1] and has['body']):
                    break
                new = step[new]
            else:
                new = (True, True)
        for key, val in zip(('sketch_shown', 'body_shown'), new):
            state[key] = val
            state[_MIRROR_KEY[key]] = val
            _home_follow(state, key, val)
            _sync_cmd_checkbox(key, val)
        _write_shared_state(state)
        apply_policy(design)
        _log('CYCLE_VISIBILITY', 'reverse=' + str(reverse), 'sketch_shown=' + str(new[0]), 'body_shown=' + str(new[1]))
        return new
    except Exception as _e:
        _log('CYCLE_VISIBILITY ERROR', _e, _traceback.format_exc())
        return (True, True)


# ── Body visibility across sketch edits ───────────────────────────────────────
#
# A sketch edit is its own context for bodies, bracketed by two forced defaults:
#   • enter → bodies ON and UN-isolated: the whole model shows (Section Sketch slices
#             it at the plane and draws every body's references) — 2026-10-06, was
#             bodies OFF. The un-isolation is the Toggle Isolation kind (transient):
#             pressing Toggle Isolation in the sketch re-isolates.
#   • exit  → bodies ON, and isolated again.
# Exiting a sketch lands you back in a component, and arriving in a component
# means the component defaults: bodies only (sketches too where no bodies would
# show, its own or the level-up fallback's).  Whatever the
# body toggle was set to before or during the edit does NOT outlive the edit —
# the toggle is transient and the sketch is where it expires.
#
# This deliberately overrides _resolve_transient, which keys the body reset on
# the COMPONENT alone and so would otherwise carry a bodies-off toggle straight
# through enter/exit (the failure that made bodies-off stick after toggling them
# on and back off inside the sketch: nothing there ever restored the default).
# Sketches take the arrival default on exit via exit_sketch_restore_sketches.

ENTER_UNISOLATED_KEY = 'enter_unisolated'


def enter_sketch_body_default(design):
    """On sketch ENTER: bodies ON and isolation OFF for the edit.  Call before
    apply_policy so that is what gets rendered.  The occurrences are un-isolated here
    (physically, as Toggle Isolation does); the flag itself is set by
    _resolve_transient, which resets isolate_off on entering the sketch's context and
    takes ENTER_UNISOLATED_KEY as that reset's value.  Not inside a master session —
    it puts its own pre-hop view state back."""
    try:
        state = _read_shared_state()
        state['body_shown']     = True
        state['bodies_visible'] = True    # legacy Browser-sync mirror
        unisolate = not master_session(design)
        state[ENTER_UNISOLATED_KEY] = unisolate
        if unisolate:
            state['widened_for'] = ''
        _write_shared_state(state)
        if unisolate and not bool(state.get('isolate_off', False)):
            _unisolate_active(design)
        _sync_isolate_checkbox(not unisolate)
    except Exception as _e:
        _log('ENTER_SKETCH_BODY ERROR', _e, _traceback.format_exc())


def _sync_isolate_checkbox(isolated):
    try:
        cd = adsk.core.Application.get().userInterface.commandDefinitions.itemById(
            'ToggleIsolationCmd')
        if cd:
            cd.isChecked = bool(isolated)
    except Exception:
        pass


# ── New sketch keeps the cycle position ───────────────────────────────────────
#
# Opening an EXISTING sketch takes the entry defaults above. A NEW one (Sketch: New,
# via the value window's plane box) instead carries on from wherever Cycle Visibility
# was when the command ran: bodies only stays bodies only, both on stays both on,
# sketches only stays sketches only. The position is captured when the box opens,
# armed when the sketch is actually made (a cancelled box arms nothing), and consumed
# by _resolve_transient on the very next sketch context — so it can only ever reach
# the sketch that was just created, and a failed entry lets it lapse.

NEW_SKETCH_CARRY_KEY  = 'new_sketch_carry'
_NEW_SKETCH_CARRY_TTL = 10.0     # seconds from arming to the sketch opening


def cycle_position():
    """The current (sketch_shown, body_shown), for arm_new_sketch_carry later."""
    state = _read_shared_state()
    return (bool(state.get('sketch_shown', False)),
            bool(state.get('body_shown', True)))


def arm_new_sketch_carry(position):
    """The sketch has been made and is about to open: have it open at position."""
    try:
        if position is None:
            return
        state = _read_shared_state()
        state[NEW_SKETCH_CARRY_KEY] = {'sketch': bool(position[0]),
                                       'body':   bool(position[1]),
                                       'at':     _time.time()}
        _write_shared_state(state)
    except Exception as _e:
        _log('ARM_NEW_SKETCH_CARRY ERROR', _e, _traceback.format_exc())


def exit_sketch_restore_body(design):
    """On sketch EXIT: bodies back to the component default (ON), whatever the
    toggle was doing before or during the edit.  Call before the exit
    apply_policy."""
    try:
        state = _read_shared_state()
        state['body_shown']     = True
        state['bodies_visible'] = True    # legacy Browser-sync mirror
        # Un-isolated (the entry default, or Toggle Isolation in the sketch): leaving
        # lands back in the component isolated. Nothing else re-isolates here — the
        # active component doesn't change, so no nav add-in runs.
        reisolate = bool(state.get('isolate_off', False))
        state['isolate_off'] = False
        state.pop(ENTER_UNISOLATED_KEY, None)
        _write_shared_state(state)
        if reisolate:
            _reisolate_active(design)
            _sync_isolate_checkbox(True)
    except Exception as _e:
        _log('EXIT_SKETCH_BODY ERROR', _e, _traceback.format_exc())


# ── Isolation toggle ──────────────────────────────────────────────────────────
#
# Unlike bodies/sketches (folder bulbs, which apply_policy re-applies every call)
# isolation lives in the OCCURRENCE light bulbs, which apply_policy never touches
# — the nav add-ins (Go Up / Go Down / Go to Top) deliberately restore_snapshot
# AFTER isolating to reveal specific levels, so apply_policy must stay folder-only
# or it would clobber them.  The isolate toggle therefore does its own physical
# un-isolate/re-isolate here; the transient flag (isolate_off) resets logically in
# _resolve_transient on the next activation, at which point the Browser/nav has
# already physically re-isolated onto the new active occurrence.

def _unisolate_active(design):
    """Show everything the user hasn't hidden: restore the saved snapshot at every
    level from root down to the active occurrence (brings sibling + ancestor
    occurrences back on, minus anything the user themself hid).  "Active" is the
    focus — the original sketch's occurrence while a master session's master is open."""
    active_occ = _focus_occ(design)
    restore_snapshot(design, None)
    if active_occ:
        path = []
        cur = active_occ
        while cur is not None:
            path.append(cur)
            try:
                cur = cur.assemblyContext
            except Exception:
                break
        for occ in reversed(path):
            restore_snapshot(design, occ)


def _reisolate_active(design):
    """Re-hide everything outside the active subtree — back to the isolated
    default (no-op at root, where there is nothing to isolate).  Focus-aware, as
    _unisolate_active."""
    active_occ = _focus_occ(design)
    if active_occ:
        isolate_occurrence_no_snapshot(design, active_occ)


def toggle_isolate(design):
    """Toggle isolation of the active component: isolated ↔ off.

      isolated — only the active subtree shows (the default), widened onto the
                 parent while the body or sketch toggle falls back a level (see
                 apply_policy) — that used to be a step of this cycle.
      off      — everything the user hasn't hidden.

    Mirrors toggle_bodies/toggle_sketches: off is TRANSIENT — it reverts to
    isolated the moment the active component changes (the flag is reset in
    _resolve_transient and the new component is physically re-isolated by whoever
    activated it).

    One exception: in a sketch edit with bodies AND sketches both toggled off,
    un-isolating would show nothing, so the press turns bodies on instead (the
    bodies/sketches cycle step that gives isolation something to act on) and
    leaves isolation alone.  From there it toggles isolation as usual.

    Returns the ISOLATED sense (True == isolated) so the toolbar checkbox reads
    "checked = isolated", with isolated as the default-on state.
    """
    try:
        apply_policy(design)  # sync the flags to the current context (resets on change)
        state      = _read_shared_state()
        body_off   = not bool(state.get('body_shown',   _flag_default(design, 'body_shown')))
        sketch_off = not bool(state.get('sketch_shown', _flag_default(design, 'sketch_shown')))

        # Inside a sketch edit both toggles may legitimately be off — the edited
        # sketch always shows, so there is no empty screen to guard against and
        # _toggle_flag's mutual exclusion doesn't apply.  But un-isolating from
        # there reveals NOTHING: once isolation is off apply_policy gates every
        # component's body and sketch folder on the two flags, so the press would
        # be a visual no-op.  Instead it takes the step of the bodies/sketches
        # cycle that gives isolation something to act on — bodies ON, isolation
        # untouched, exactly as if Toggle Body Visibility had been pressed.  With
        # bodies showing, the next press is an ordinary isolate toggle again.
        if body_off and sketch_off and is_editing_sketch(design):
            state['body_shown'] = True
            state[_MIRROR_KEY['body_shown']] = True
            _sync_cmd_checkbox('body_shown', True)
            _write_shared_state(state)
            apply_policy(design)
            isolated = not bool(state.get('isolate_off', False))
            _log('TOGGLE_ISOLATE', 'bodies on (nothing to isolate onto)',
                 'isolated' if isolated else 'off')
            return isolated

        new_off = not bool(state.get('isolate_off', False))
        state['isolate_off'] = new_off
        state['widened_for'] = ''   # un-isolating supersedes it; re-isolating resets it

        # Outside a sketch edit, both bodies and sketches off leaves nothing to
        # isolate onto — same "not an empty screen" guard as _toggle_flag's mutual
        # exclusion, applied here because isolating/un-isolating can newly expose
        # that combination without either flag having been touched itself.
        if body_off and sketch_off:
            state['sketch_shown'] = True
            state[_MIRROR_KEY['sketch_shown']] = True
            _sync_cmd_checkbox('sketch_shown', True)

        _write_shared_state(state)
        if new_off:
            _unisolate_active(design)
        else:
            _reisolate_active(design)
        _log('TOGGLE_ISOLATE', 'off' if new_off else 'isolated')
        apply_policy(design)   # refresh folder bulbs for the newly (un)hidden set
        return not new_off   # checkbox: True == isolated
    except Exception as _e:
        _log('TOGGLE_ISOLATE ERROR', _e, _traceback.format_exc())
        return True


def set_sketches_shown(design, value=True):
    """Force the transient sketch_shown flag for the current (non-editing) context
    and PIN policy_ctx so apply_policy won't immediately reset it.

    Used on sketch EXIT to bring the other sketches back on by default, overriding
    the component's depth-based default (a non-leaf component would otherwise hide
    them again the moment you leave the sketch).  Because policy_ctx is pinned to
    the current component-only context, the forced value sticks until you navigate
    to a different component (or re-enter a sketch), at which point the normal
    depth-based reset takes over again.

    Bodies are deliberately LEFT ALONE here: the body toggle survives jumping in
    and out of a sketch within the same component (see _resolve_transient, which
    keys the body reset on the component alone), so forcing body_shown back on
    would wrongly undo a bodies-off toggle the moment you finish the sketch."""
    try:
        active_id = _comp_id(design.activeComponent)
        ctx_key   = '{}|{}'.format(active_id, '')   # '' = not editing a sketch
        state = _read_shared_state()
        state['policy_ctx']       = ctx_key
        state['sketch_shown']     = bool(value)
        state['sketches_visible'] = bool(value)
        _write_shared_state(state)
        apply_policy(design)
    except Exception as _e:
        _log('SET_SKETCHES_SHOWN ERROR', _e, _traceback.format_exc())


def exit_sketch_restore_sketches(design):
    """On sketch EXIT: the other sketches take the component-arrival default —
    OFF (bodies only) where bodies would show (its own or the level-up fallback's),
    ON where none would (see _resolve_transient) — pinned by set_sketches_shown.  Call after
    exit_sketch_restore_body.  Stamps the exit first so this and the later
    re-activations don't read Fusion's lagging "sketch open" signal."""
    note_sketch_exit()
    set_sketches_shown(design, not _focus_has_bodies(design))


def restore_sketch_bulbs(design, comp=None):
    """Restore comp's per-sketch bulbs to their deliberate state (active comp by
    default) — everything back on except the sketches the user actually hid.

    Call on sketch exit: while editing, the OTHER sketches may have been hidden
    via their individual bulbs (see _apply_sketch_edit_visibility); the folder is
    the single authority once the edit ends, so the per-sketch bulbs must be
    restored or those sketches would stay stranded-off.  Deliberate hides are
    recorded as intent (see the leaf section) and survive the restore.

    Every bulb written here is stamped into the mirror, like any other write of
    ours. The edit override left the mirror holding its transient OFFs, so without
    the stamp the exit's apply_policy read these restores back as somebody else
    turning the sketches on and ADOPTed every one."""
    try:
        comp = comp or design.activeComponent
        if comp is None:
            return
        lin = _lineage(design)
        if groups_pending(lin):
            return          # see groups_pending — never write leaf bulbs blind
        data    = _load()
        rec     = _leaf_rec(data, lin)
        hidden  = set(rec['hidden']) if rec else set()
        groups  = hidden_group_members(lin)
        comp_id = _comp_id(comp)
        dirty   = False
        for key, sk in _keyed(comp_id, 'sk', comp.sketches):
            if _is_bounds_sketch(sk) or _is_master_sketch(sk):
                continue          # never restored on — see _is_bounds_sketch
            _set_bulb(sk, key not in hidden and not groups.get(key))
            if rec is not None:
                try:
                    cur = bool(sk.isLightBulbOn)
                except Exception:
                    continue
                if rec['mirror'].get(key) != cur:
                    rec['mirror'][key] = cur
                    dirty = True
        if dirty:
            _save(data)
    except Exception as _e:
        _log('RESTORE_SKETCH_BULBS ERROR', _e, _traceback.format_exc())


def _set_folder(comp, prop, value):
    try:
        if getattr(comp, prop) != value:
            setattr(comp, prop, value)
    except Exception:
        pass


# ── Origin geometry during sketch edit ────────────────────────────────────────
#
# Origins are always hidden by apply_policy.  The single exception is while a
# sketch drawn on one of a component's origin construction planes is being
# edited: then the two in-plane axes are shown (and only those — no origin point,
# no third axis, no planes), so you have reference axes to sketch against.

def _set_bulb(entity, value):
    try:
        if entity is not None and entity.isLightBulbOn != value:
            entity.isLightBulbOn = value
    except Exception:
        pass


_AID_PROPS = ('areDimensionsShown', 'areConstraintsShown', 'isConstructionGeometryShown')
_AID_LOOK_KEY = 'sketch_aid_look'


def remember_sketch_aid_look(sketch, dims, cons, constr):
    """Record the annotation look Sketch Dimensions just set on the sketch being
    edited, so apply_policy re-applies THAT rather than forcing all three back on.

    Without this, anything that re-ran the policy mid-edit (Cycle Visibility calls
    it twice, the Browser calls it on every navigation) put dimensions, constraints
    and construction geometry straight back on, because _apply_sketch_edit_visibility
    forces them on for the edited sketch on every single run.  The look is still not
    a preference: it is stamped with the sketch it was set on and cleared on every
    sketch enter and exit, so it lives exactly as long as the edit it was set in."""
    try:
        native = (sketch.nativeObject or sketch) if sketch else None
        if native is None:
            return
        state = _read_shared_state()
        try:
            tok = native.entityToken
        except Exception:
            tok = ''
        state[_AID_LOOK_KEY] = {'tok': tok, 'name': native.name,
                                'dims': bool(dims), 'cons': bool(cons),
                                'constr': bool(constr)}
        _write_shared_state(state)
    except Exception as _e:
        _log('REMEMBER_AID_LOOK ERROR', _e, _traceback.format_exc())


def clear_sketch_aid_look():
    """Forget the remembered look — on sketch enter (so a newly opened sketch always
    starts with everything shown) and on exit."""
    try:
        state = _read_shared_state()
        if state.pop(_AID_LOOK_KEY, None) is not None:
            _write_shared_state(state)
    except Exception:
        pass


def _aid_look_for(sk):
    """The remembered look as a {prop: bool} dict, but only if it was recorded for
    THIS sketch (token or name).  None otherwise, which means "all three on" — so a
    look can never leak onto the next sketch you open, however you opened it."""
    try:
        look = _read_shared_state().get(_AID_LOOK_KEY)
        if not isinstance(look, dict):
            return None
        match = False
        if look.get('tok'):
            try:
                match = (sk.entityToken == look['tok'])
            except Exception:
                pass
        if not match and look.get('name'):
            try:
                match = (sk.name == look['name'])
            except Exception:
                pass
        if not match:
            return None
        return {'areDimensionsShown': bool(look.get('dims', True)),
                'areConstraintsShown': bool(look.get('cons', True)),
                'isConstructionGeometryShown': bool(look.get('constr', True))}
    except Exception:
        return None


def _set_sketch_aids(sk, on):
    """Show/hide a sketch's dimensions, constraints and construction geometry — the
    annotations that should ONLY ever be visible for the sketch you're actively editing.
    apply_policy forces these OFF on every non-edited sketch on every run, so they can
    never linger after you exit a sketch no matter how you exit (Finish Sketch, Go Up,
    activating another component, clicking a parent in the browser).  Guarded so it only
    writes on a real change.

    on=True means "the sketch being edited": the remembered look wins there if Sketch
    Dimensions set one during this edit (see remember_sketch_aid_look), so re-running
    the policy no longer undoes it.  on=False is unconditional."""
    look = _aid_look_for(sk) if on else None
    for prop in _AID_PROPS:
        want = look[prop] if look else on
        try:
            if getattr(sk, prop) is not want:
                setattr(sk, prop, want)
        except Exception:
            pass


def reveal_sketch_axes(design):
    """If a sketch on an origin plane is being edited, show its two in-plane axes.

    No-op for sketches on faces / offset planes (origin stays fully hidden).
    Safe to call repeatedly.
    """
    if adsk is None:
        return
    try:
        sketch = active_edit_sketch(design)
        if not sketch:
            return
        native = sketch.nativeObject or sketch
        comp   = native.parentComponent
        plane  = native.referencePlane
        if comp is None or plane is None:
            return
        ax_x, ax_y, ax_z = (comp.xConstructionAxis,
                            comp.yConstructionAxis,
                            comp.zConstructionAxis)
        try:
            ptok = plane.entityToken
        except Exception:
            ptok = None
        plane_axes = {
            comp.xYConstructionPlane.entityToken: (ax_x, ax_y),
            comp.xZConstructionPlane.entityToken: (ax_x, ax_z),
            comp.yZConstructionPlane.entityToken: (ax_y, ax_z),
        }
        pair = plane_axes.get(ptok)
        if not pair:
            return  # face / offset plane — leave origin hidden
        _set_folder(comp, 'isOriginFolderLightBulbOn', True)
        for ax in (ax_x, ax_y, ax_z):
            _set_bulb(ax, ax in pair)
        _set_bulb(comp.originConstructionPoint, False)
        for pl in (comp.xYConstructionPlane,
                   comp.xZConstructionPlane,
                   comp.yZConstructionPlane):
            _set_bulb(pl, False)
    except Exception as _e:
        _log('REVEAL_SKETCH_AXES ERROR', _e, _traceback.format_exc())


def restore_origin_default(design, comp=None):
    """Return a component's origin geometry to Fusion's default hidden state:
    folder OFF, but axes/point/planes bulbs back ON underneath (so nothing is
    permanently blanked).  Call on sketch exit.  Defaults to the active comp.

    Unless the Browser's Origin group has something of this component's turned on
    (origin_folder_wanted) — then the folder stays open and the bulbs are left as
    the group set them, rather than the edit-exit blanking them all back on and
    disagreeing with what origins_shown still remembers wanting.
    """
    try:
        comp = comp or design.activeComponent
        if comp is None:
            return
        if origin_folder_wanted(_comp_id(comp)):
            _set_folder(comp, 'isOriginFolderLightBulbOn', True)
            return
        _set_folder(comp, 'isOriginFolderLightBulbOn', False)
        for entity in (comp.xConstructionAxis, comp.yConstructionAxis,
                       comp.zConstructionAxis, comp.originConstructionPoint,
                       comp.xYConstructionPlane, comp.xZConstructionPlane,
                       comp.yZConstructionPlane):
            _set_bulb(entity, True)
    except Exception as _e:
        _log('RESTORE_ORIGIN_DEFAULT ERROR', _e, _traceback.format_exc())


# ── Sketch editing aids (dimensions / constraints / construction geometry) ─────
#
#   enter a sketch → show dimensions + constraints (+ construction geometry, so
#                    you can edit it)
#   exit  a sketch → hide construction geometry + constraints in that sketch
#
# On exit the sketch is no longer the activeEditObject, so the edited sketch's
# token is remembered on enter and resolved again on exit.

def remember_edit_sketch(sketch):
    """Persist the sketch now being edited so the policy can keep it visible even
    when design.activeEditObject later raises (sketch nested in an occurrence).
    Stores BOTH the native entity token and the name — the name is the resilient
    fallback when token resolution fails."""
    try:
        native = (sketch.nativeObject or sketch) if sketch else None
        data = _load()
        data['__edit_sketch__']      = native.entityToken if native else ''
        try:
            data['__edit_sketch_name__'] = native.name if native else ''
        except Exception:
            data['__edit_sketch_name__'] = ''
        _save(data)
        _touch_edit_stamp()
    except Exception:
        pass


def recall_edit_sketch(design):
    """Resolve the remembered edited sketch.  Tries the stored entity token first,
    then falls back to matching the stored name against the active component's
    sketches (token resolution can fail in deep-occurrence edits)."""
    try:
        data = _load()
        tok = data.get('__edit_sketch__', '')
        if tok:
            try:
                ents = design.findEntityByToken(tok)
                sk = adsk.fusion.Sketch.cast(ents[0]) if ents else None
                if sk:
                    return sk
            except Exception:
                pass
        name = data.get('__edit_sketch_name__', '')
        if name:
            try:
                for sk in design.activeComponent.sketches:
                    if sk.name == name:
                        return sk
            except Exception:
                pass
    except Exception:
        pass
    return None


# When the edit was opened, as a file mtime rather than a key in the snapshot JSON.
# is_editing_sketch() consults this on every call that isn't already a yes, and that
# includes the Browser add-in's ~14 Hz sync tick — so it has to cost a stat(), not a
# re-parse of a 45 KB document.
_EDIT_STAMP_FILE = '/tmp/fusion_sketch_edit_open'
EDIT_SETTLE_SECS = 3.0


def _touch_edit_stamp():
    try:
        with open(_EDIT_STAMP_FILE, 'w'):
            pass
    except Exception:
        pass
    try:
        os.remove(_EXIT_STAMP_FILE)
    except Exception:
        pass


# The mirror image for a real exit (not a master hop): note_sketch_exit() stamps it
# and is_editing_sketch() says "not editing" until Fusion itself reports the sketch
# closed (SketchStop disabled — the stamp has done its job and is removed), a sketch
# has been opened since (_touch_edit_stamp removes it), or EXIT_SETTLE_SECS pass.
#
# Not a fixed two seconds: on a big document the lag outlasted it. Go Up from a
# sketch re-activates on a deferred event, and its apply_policy ran ~2.5 s after the
# exit with SketchStop still enabled, so it took the "a sketch is open, can't tell
# which" path and switched EVERY sketch on — the user's hides included — until the
# next navigation.
_EXIT_STAMP_FILE = '/tmp/fusion_sketch_edit_closed'
EXIT_SETTLE_SECS = 15.0


def note_sketch_exit():
    try:
        with open(_EXIT_STAMP_FILE, 'w'):
            pass
    except Exception:
        pass


def exit_is_settling(enabled=None):
    """True while a sketch exit is still in flight. `enabled`: SketchStop's
    isEnabled as just read — False means Fusion has caught up, which ends it."""
    try:
        t = os.path.getmtime(_EXIT_STAMP_FILE)
    except OSError:
        return False
    if _time.time() - t >= EXIT_SETTLE_SECS:
        return False
    try:
        if os.path.getmtime(_EDIT_STAMP_FILE) > t:
            return False
    except OSError:
        pass
    if enabled is False:
        try:
            os.remove(_EXIT_STAMP_FILE)
        except Exception:
            pass
        return False
    return True


def _clear_edit_stamp():
    try:
        os.remove(_EDIT_STAMP_FILE)
    except Exception:
        pass


def edit_is_settling():
    """True for a moment after a sketch edit was opened.

    SketchStop.isEnabled is the authoritative "a sketch is open" test (see
    is_editing_sketch) at every other moment, but it is still False for a beat
    AFTER SketchActivate has terminated — and apply_policy runs inside that beat,
    from the very handler that just opened the sketch. It therefore took the
    not-editing path, and the per-component loop there switched every recorded-hidden
    sketch off — including the one just opened. Opening a hidden sketch left you
    drawing into an invisible one, for the whole edit, because nothing re-applied the
    policy once the flag settled.

    Go Down's terminated handler works around this for its own call by passing
    force_edit_sketch, but only when it can identify the sketch from the timeline
    marker, and the marker frequently cannot (marker_sketch=None in the logs). The
    sketch remembered at entry always can, so this says when to trust it.
    """
    try:
        return (_time.time() - os.path.getmtime(_EDIT_STAMP_FILE)) < EDIT_SETTLE_SECS
    except OSError:
        return False


def clear_edit_sketch():
    try:
        data = _load()
        data.pop('__edit_sketch__', None)
        data.pop('__edit_sketch_name__', None)
        _save(data)
    except Exception:
        pass
    _clear_edit_stamp()


def show_sketch_aids(design):
    """On sketch enter: show dimensions, constraints and construction geometry
    for the sketch now being edited."""
    if adsk is None:
        return
    try:
        clear_sketch_aid_look()   # a new sketch starts with everything shown
        sk = active_edit_sketch(design)
        if not sk:
            return
        for prop in ('areDimensionsShown', 'areConstraintsShown',
                     'isConstructionGeometryShown'):
            try:
                if getattr(sk, prop) is not True:
                    setattr(sk, prop, True)
            except Exception:
                pass
        remember_edit_sketch(sk)
    except Exception as _e:
        _log('SHOW_SKETCH_AIDS ERROR', _e, _traceback.format_exc())


def hide_sketch_aids(design):
    """On sketch exit: hide dimensions, constraints and construction geometry in
    the sketch that was just being edited — so you never see the annotations of a
    sketch you're not in.  Symmetric with show_sketch_aids (which shows all three
    on enter).  Note `areDimensionsShown` is the API's "displayed when the sketch
    is NOT active" knob, so without it the dimensions stayed visible after exit."""
    try:
        sk = recall_edit_sketch(design)
        if sk:
            for prop in ('areDimensionsShown', 'isConstructionGeometryShown',
                         'areConstraintsShown'):
                try:
                    if getattr(sk, prop) is not False:
                        setattr(sk, prop, False)
                except Exception:
                    pass
        clear_sketch_aid_look()
        clear_edit_sketch()
    except Exception as _e:
        _log('HIDE_SKETCH_AIDS ERROR', _e, _traceback.format_exc())


def run(context):
    pass


def stop(context):
    pass
