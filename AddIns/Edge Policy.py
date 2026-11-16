"""
Edge Policy — per-component "hide edges when the component is not active".

A component can be flagged (from the Browser window's press-and-hold menu). While
flagged, every body in that component AND in every component nested under it draws
WITHOUT edges — unless the active component is that component or one of its
descendants, in which case edges come back for the whole subtree. So the flag picks
the level of the hierarchy at which edges drop out; a flagged component nested in a
flagged one still hides its own subtree until you enter it. The point is to stop
dense internal geometry (ribs, perforated substrates) bleeding its edges through
thin outer panels when you're zoomed out.

The finer form (the menu's Hide Edges flyout) names the OUTER components instead:
`hideEdgesWhen` drops a component's edges while any of the chosen components is drawn
in the viewport — see edge_triggers / _trigger_parts. The two combine: edges are off
if either says so.

Editing a SKETCH counts as being inside the component that owns it, whatever the
active component happens to be: the bodies you are sketching against are exactly
the ones whose edges you need, and entering a sketch does not necessarily activate
its owner (a timeline double-click doesn't, for one). The caller supplies the
owning component — see `apply`.

HOW IT WORKS
------------
Fusion keeps each BRep body's edges in their own scene-graph node, separate from
its faces:

    Ns::BREP::Body::facesNode()  -> StrongRef<OGS::Graphics::GroupNode> @ this+0x2C0
    Ns::BREP::Body::edgesNode()  -> StrongRef<OGS::Graphics::GroupNode> @ this+0x2E8

Hiding edges is therefore the same operation Fusion uses for a light bulb, applied
one level deeper — a NodeChange on the edges node:

    NodeChange nc(edgesNode, false); nc.visibility(false, true, 0x0F); nc.~NodeChange();

which is exactly what Ns::BREP::Body::realizeAppearance does internally. It is a
RENDER-time change: instant, no re-tessellation, no timeline roll. Measured
reversible to the pixel, and it survives activate / isolate / light-bulb toggles /
a full timeline roll. It is runtime-only: nothing is written to the document, so
it must be re-applied when a document is opened.

The only unexported piece is the hop from a Fusion API BRepBody to the internal
Ns::BREP::Body. That pointer path is DISCOVERED AT RUNTIME (never hardcoded) by
fingerprinting against the exported `vtable for Ns::BREP::Body`, so a Fusion
update that shifts object layout is handled rather than silently mis-pointing. If
discovery fails, every entry point becomes a no-op and edges behave stock.

SAFETY
------
Every memory read goes through mach_vm_read_overwrite, which returns an error code
for unmapped memory instead of faulting. Nothing is dereferenced unchecked, and a
pointer is only ever CALLED after its vptr is confirmed to be Ns::BREP::Body's.
"""

import ctypes
import json
import os
import threading
import time as _time

# Discovery bounds — see _Native.discover.
_DISCOVER_MAX_TRIES = 4
_DISCOVER_COOLDOWN = 5.0
# Bounded retry when a body can't be resolved yet (scene nodes not built at
# document-open). Retries are spaced and finite; after this we latch and stop.
_RETRY_MAX = 12
_RETRY_MIN_GAP = 0.5

# ── persistence ──────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(_HERE, 'Edge Policy.json')
_io_lock = threading.Lock()


def _load_all():
    try:
        with open(_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_all(data):
    tmp = _FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, _FILE)


def _normalise(entry):
    """Accept both schemas.

    v1 was a bare list of component ids meaning 'hide edges'. v2 is
    {compId: {"hideEdges": bool, ...}} so a component can carry more
    than one setting. Old files keep working and get rewritten on next save.
    """
    if isinstance(entry, list):
        return {cid: {'hideEdges': True} for cid in entry}
    if isinstance(entry, dict):
        out = {}
        for cid, val in entry.items():
            if isinstance(val, dict):
                out[cid] = val
            elif val:                      # tolerate {compId: true}
                out[cid] = {'hideEdges': True}
        return out
    return {}


def settings_for(doc_name):
    with _io_lock:
        return _normalise(_load_all().get(doc_name, {}))


def _write_entry(doc_name, comp_id, key, value, drop_if):
    """`value` may be a function of the stored value, so a read-modify-write (adding
    one trigger to a list) happens under the same lock as the write."""
    with _io_lock:
        data = _load_all()
        entry = _normalise(data.get(doc_name, {}))
        rec = dict(entry.get(comp_id, {}))
        if callable(value):
            value = value(rec.get(key))
        if drop_if(value):
            rec.pop(key, None)
        else:
            rec[key] = value
        if rec:
            entry[comp_id] = rec
        else:
            entry.pop(comp_id, None)
        if entry:
            data[doc_name] = entry
        else:
            data.pop(doc_name, None)
        _save_all(data)
    return value


def flagged_ids(doc_name):
    """Component ids flagged 'hide edges when not active' in this document."""
    return {cid for cid, rec in settings_for(doc_name).items() if rec.get('hideEdges')}


def is_flagged(doc_name, comp_id):
    return comp_id in flagged_ids(doc_name)


def set_flag(doc_name, comp_id, on):
    _write_entry(doc_name, comp_id, 'hideEdges', bool(on), lambda v: not v)
    return bool(on)


def toggle(doc_name, comp_id):
    return set_flag(doc_name, comp_id, not is_flagged(doc_name, comp_id))


# ── hide edges while other components are visible ────────────────────────────
# `hideEdgesWhen`: a list of component ids (Component.id — rename-proof). While ANY of
# them is drawn in the viewport, this component and its subtree draw without edges;
# when none is, edges are back. Purely visibility-driven: being inside the component
# doesn't override it — isolating it (which hides the others) is what does. See
# _trigger_parts for what "drawn" means. An id whose component is gone (deleted, or
# not yet built at a rolled-back marker) just never fires; it isn't pruned.
def edge_triggers(doc_name):
    """{component id: frozenset(trigger component ids)} for this document."""
    out = {}
    for cid, rec in settings_for(doc_name).items():
        ts = rec.get('hideEdgesWhen')
        if isinstance(ts, list) and ts:
            out[cid] = frozenset(str(t) for t in ts)
    return out


def set_trigger(doc_name, comp_id, trigger_id, on):
    def edit(cur):
        ts = [t for t in (cur if isinstance(cur, list) else []) if t != trigger_id]
        if on:
            ts.append(trigger_id)
        return sorted(ts)
    _write_entry(doc_name, comp_id, 'hideEdgesWhen', edit, lambda v: not v)
    return bool(on)


# ── hide entirely while not active ───────────────────────────────────────────
# The exact fix for fully-enclosed internal geometry: not showing what can't
# legitimately be seen means no bleed at any angle and no punch-through, and it
# reappears when you go inside.
def hidden_ids(doc_name):
    return {cid for cid, rec in settings_for(doc_name).items() if rec.get('hideBody')}


def is_hidden(doc_name, comp_id):
    return comp_id in hidden_ids(doc_name)


def set_hidden(doc_name, comp_id, on):
    _write_entry(doc_name, comp_id, 'hideBody', bool(on), lambda v: not v)
    return bool(on)


def toggle_hidden(doc_name, comp_id):
    return set_hidden(doc_name, comp_id, not is_hidden(doc_name, comp_id))


# ── native bridge ────────────────────────────────────────────────────────────
_SYMS = {
    'vtable': b'_ZTVN2Ns4BREP4BodyE',
    'edges':  b'_ZNK2Ns4BREP4Body9edgesNodeEv',
    'faces':  b'_ZNK2Ns4BREP4Body9facesNodeEv',
    'ctor':   b'_ZN2Ns5Scene10NodeChangeC1EPN3OGS8Graphics9GroupNodeEb',
    'vis':    b'_ZN2Ns5Scene10NodeChange10visibilityEbbh',
    'dtor':   b'_ZN2Ns5Scene10NodeChangeD1Ev',
}

# BFS limits for path discovery. Generous enough to find it (the real path is 4
# hops), bounded enough that a miss costs a fraction of a second.
_SCAN_WORDS = 128
_FRONTIER_CAP = 3000
_MAX_DEPTH = 6
_READ_CAP = 2000000


class _Native(object):
    """Lazily-initialised. `ok` False means every operation is a no-op."""

    def __init__(self):
        self.ok = False
        self.reason = 'not initialised'
        self.path = None          # discovered pointer path, e.g. (0x0, 0x30, 0x368, 0x3B8)

        # Discovery walks arbitrary memory, so it must be rare. Unbounded re-derivation
        # (e.g. once per sync tick while the scene is in flux during an add-in reload)
        # is dangerous — that is what crashed Fusion once already.
        self._discover_tries = 0
        self._discover_last = 0.0
        try:
            self._init_syms()
        except Exception as exc:                                  # never propagate
            self.reason = 'symbol init failed: %r' % (exc,)

    def _init_syms(self):
        lib = ctypes.CDLL(None)
        lib.dlsym.restype = ctypes.c_void_p
        lib.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.mach_task_self.restype = ctypes.c_uint32
        lib.mach_vm_read_overwrite.restype = ctypes.c_int
        lib.mach_vm_read_overwrite.argtypes = [
            ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64,
            ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
        self._lib = lib
        self._task = lib.mach_task_self()
        self._b8 = ctypes.create_string_buffer(8)
        self._b8a = ctypes.addressof(self._b8)
        self._osz = ctypes.c_uint64(0)

        rtld = ctypes.c_void_p(-2)
        addr = {k: (lib.dlsym(rtld, s) or 0) for k, s in _SYMS.items()}
        missing = [k for k, v in addr.items() if not v]
        if missing:
            self.reason = 'missing symbols: %s' % ','.join(missing)
            return

        # Refuse to run unless the safe-read primitive demonstrably rejects a
        # known-unmapped address. Without this guarantee we must not walk memory.
        if self._raw(0x00007F0000000000) is not None or self._raw(self._b8a) is None:
            self.reason = 'unsafe memory primitive'
            return

        self._vt_lo = addr['vtable']
        self._vt_hi = addr['vtable'] + 0x800
        self._edges_fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(addr['edges'])
        self._faces_fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(addr['faces'])
        self._ctor = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(addr['ctor'])
        self._visf = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_ubyte)(addr['vis'])
        self._dtor = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(addr['dtor'])
        self.ok = True
        self.reason = 'ok'

    # -- safe memory access --------------------------------------------------
    def _raw(self, addr):
        kr = self._lib.mach_vm_read_overwrite(
            self._task, ctypes.c_uint64(addr), 8,
            ctypes.c_uint64(self._b8a), ctypes.byref(self._osz))
        if kr != 0 or self._osz.value != 8:
            return None
        return int.from_bytes(self._b8.raw[:8], 'little')

    def _rq(self, addr):
        """Safe 8-byte read. Deliberately UNMETERED.

        The read budget belongs to a single discover() walk, not to the process.
        Metering here made it a lifetime odometer: after enough ordinary resolves
        the cap tripped and every read returned None forever — and because
        discover() reset the counter only after _swig_base() (which reads), it
        could never recover. Symptom was silent, total failure.
        """
        if not addr or addr < 0x10000 or addr >= (1 << 47):
            return None
        return self._raw(addr)

    def _swig_base(self, api_obj):
        try:
            swig = api_obj.this
        except Exception:
            return None
        for off in (16, 24, 8, 32):
            val = self._rq(id(swig) + off)
            if val and self._rq(val) is not None:
                return val
        return None

    def _is_body(self, ptr):
        vptr = self._rq(ptr)
        return vptr is not None and self._vt_lo <= vptr < self._vt_hi

    # -- path discovery ------------------------------------------------------
    def _follow(self, api_body, path):
        cur = self._swig_base(api_body)
        for off in path:
            if cur is None:
                return None
            cur = self._rq(cur + off)
        return cur if cur is not None and self._is_body(cur) else None

    def _validated(self, path, api_body, peers):
        """A path is only right if it maps DIFFERENT bodies to DIFFERENT internal
        bodies. The vtable check alone can't tell: the BFS happily finds shorter paths
        through some shared cache that land on *a* Ns::BREP::Body — just not this
        one — and every hide/show then hits the wrong body. (It did: a 3-hop
        (0x188,0x60,0x260) was latched while the real path is (0x0,0x30,0x368,0x3B8).)
        """
        seen = set()
        tokens = set()
        for b in [api_body] + list(peers or ()):
            try:
                tok = b.entityToken
            except Exception:
                tok = None
            if tok is not None:
                if tok in tokens:        # same body via another wrapper — not a peer
                    continue
                tokens.add(tok)
            p = self._follow(b, path)
            if p is None or p in seen:
                return False
            seen.add(p)
        return True

    def discover(self, api_body, peers=None):
        """Find the pointer path from a SWIG BRepBody to its Ns::BREP::Body.

        `peers` are other bodies in the design; a candidate path must resolve each of
        them to a distinct body (see _validated). With no peers there is nothing to
        cross-check, so the first candidate is taken as before.

        Hard-limited: at most _DISCOVER_MAX_TRIES attempts, never more than one per
        _DISCOVER_COOLDOWN seconds. After that we give up permanently and edges stay
        stock, which is the correct failure mode — a scan per tick is not.
        """
        if not self.ok:
            return None
        now = _time.time()
        if self._discover_tries >= _DISCOVER_MAX_TRIES:
            return None
        if now - self._discover_last < _DISCOVER_COOLDOWN:
            return None
        self._discover_last = now
        self._discover_tries += 1
        base = self._swig_base(api_body)
        if base is None:
            return None

        # Budget scoped to THIS walk only.
        budget = [_READ_CAP]

        def rd(addr):
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            return self._rq(addr)

        seen = {base}
        frontier = [(base, ())]
        for _depth in range(_MAX_DEPTH):
            nxt = []
            for addr, path in frontier:
                for i in range(_SCAN_WORDS):
                    if budget[0] <= 0:
                        return None
                    val = rd(addr + i * 8)
                    if val is None:
                        continue
                    head = rd(val)
                    if head is None:
                        continue
                    if self._vt_lo <= head < self._vt_hi:
                        cand = path + (i * 8,)
                        if self._validated(cand, api_body, peers):
                            return cand
                        continue               # wrong body: keep looking, don't descend
                    if val not in seen:
                        seen.add(val)
                        nxt.append((val, path + (i * 8,)))
            frontier = nxt[:_FRONTIER_CAP]
            if not frontier:
                break
        return None

    def resolve(self, api_body, peers=None):
        """API body -> validated Ns::BREP::Body*, or None."""
        if not self.ok:
            return None
        if self.path is None:
            self.path = self.discover(api_body, peers)
            if self.path is None:
                return None
        cur = self._swig_base(api_body)
        if cur is None:
            return None
        for off in self.path:
            cur = self._rq(cur + off)
            if cur is None:
                return None
        if not self._is_body(cur):
            # Layout moved (a Fusion update, most likely). Re-derive once; if that
            # also fails the caller gets None and edges stay stock.
            self.path = self.discover(api_body, peers)
            if self.path is None:
                return None
            cur = self._swig_base(api_body)
            for off in self.path:
                cur = self._rq(cur + off) if cur is not None else None
            if cur is None or not self._is_body(cur):
                return None
        return cur

    def edges_node(self, body_ptr):
        if not self.ok or not body_ptr:
            return None
        return self._edges_fn(ctypes.c_void_p(body_ptr)) or None

    def faces_node(self, body_ptr):
        if not self.ok or not body_ptr:
            return None
        return self._faces_fn(ctypes.c_void_p(body_ptr)) or None

    def set_node_visible(self, node_ptr, visible):
        """Show/hide any scene node (faces or edges). NodeChange -> visibility -> dtor."""
        if not self.ok or not node_ptr:
            return
        buf = ctypes.create_string_buffer(4096)   # generous; the real object is small
        nc = ctypes.addressof(buf)
        self._ctor(ctypes.c_void_p(nc), ctypes.c_void_p(node_ptr), False)
        self._visf(ctypes.c_void_p(nc), bool(visible), True, 0x0F)
        self._dtor(ctypes.c_void_p(nc))


_native = None


def native():
    global _native
    if _native is None:
        _native = _Native()
    return _native


def status():
    n = native()
    return {'ok': n.ok, 'reason': n.reason,
            'path': [hex(o) for o in n.path] if n.path else None}


# ── policy ───────────────────────────────────────────────────────────────────
# Components we've touched, so un-flagging one restores its edges.
_managed = {}          # doc_name -> set(comp_id)
_swept = set()         # doc_name -> a full sweep has run in THIS module instance
_last_sig = {}         # doc_name -> signature of the last applied state
_retry = {}            # doc_name -> [attempts, last_attempt_ts] for incomplete applies
# Gate for the body-count scan in apply() — see the comment there.
_scan_sig = {}         # doc_name -> cheap change token as of the last scan
_scan_ts = {}          # doc_name -> ts of the last scan
_SCAN_MAX_GAP = 5.0    # forced rescan interval (s), for changes the token can't see
# Tick Guard's shared (count, marker) reading, set by the loader (Browser.py) — this
# file doesn't load Tick Guard itself. None: read the timeline directly.
timeline_pos = None


def _doc_token(design):
    """Cheap stand-in for "the model changed": two property reads instead of a walk
    over every component. Any feature that adds or removes a body moves the timeline.
    Documents with no timeline (direct modelling) return a constant, so there the
    periodic forced rescan in apply() is what picks new bodies up."""
    try:
        if timeline_pos is not None:
            return timeline_pos(design)
        tl = design.timeline
        return (tl.count, tl.markerPosition)
    except Exception:
        return (-1, -1)


def _active_chain_ids(design, edit_component=None):
    """Component ids that count as "you are inside them" right now: the active
    component and all its ancestors, plus the owner of the sketch being edited.

    A flagged component shows its edges exactly when it is in this set — i.e. when
    you are inside it or inside one of its subcomponents, or when the sketch open
    for edit is one of its own.

    `edit_component`'s ancestors are deliberately NOT added. An ancestor you
    actually navigated into is already in the active chain; adding them from a
    sketch owner would need a walk over every occurrence in the assembly, on a path
    that runs at the browser sync rate.
    """
    ids = set()
    try:
        occ = getattr(design, 'activeOccurrence', None)
    except Exception:
        occ = None
    while occ:
        try:
            ids.add(occ.component.id)
            occ = occ.assemblyContext
        except Exception:
            break
    # design.activeOccurrence RAISES (InternalValidationError: occPath.valid()) for the
    # whole duration of a sketch edit, so the walk above yields NOTHING and the chain
    # collapses to the root alone — every flagged component loses its edges the moment
    # any sketch opens, including the one you are working inside. The active COMPONENT
    # stays correct throughout, so it is the reliable floor. (Its ancestors are still
    # missed in that window; a flagged component you are inside is covered, a flagged
    # ANCESTOR of it is not, and recovering those needs the occurrence path that Fusion
    # is refusing to hand out.)
    try:
        ids.add(design.activeComponent.id)
    except Exception:
        pass
    try:
        ids.add(design.rootComponent.id)
    except Exception:
        pass
    if edit_component is not None:
        try:
            ids.add(edit_component.id)
        except Exception:
            pass
    return ids


_subtree_cache = {}    # doc_name -> (key, {flagged id: frozenset(subtree ids)})


def _subtrees(design, doc_name, flags):
    """{flagged id: ids of it and every component nested under it}.

    The flag covers the whole subtree: a flagged parent hides the edges of its
    subcomponents' bodies too, until you go inside the parent. Walks
    Component.allOccurrences (nested) per flagged component, so it's cached on the
    flag set + the cheap document token and only rebuilt when either moves.
    """
    key = (frozenset(flags), _doc_token(design))
    hit = _subtree_cache.get(doc_name)
    if hit and hit[0] == key:
        return hit[1]
    out = {}
    try:
        comps = design.allComponents
    except Exception:
        comps = None
    for fid in flags:
        ids = {fid}
        try:
            comp = comps.itemById(fid) if comps is not None else None
            if comp is not None:
                for occ in comp.allOccurrences:
                    try:
                        ids.add(occ.component.id)
                    except Exception:
                        continue
        except Exception:
            pass
        out[fid] = frozenset(ids)
    _subtree_cache[doc_name] = (key, out)
    return out


_parts_cache = {}      # doc_name -> (key, {owner id: [(occ, comp id, [body proxies])]})


def _trigger_parts(design, doc_name, rules, subtrees):
    """{owner id: every placed piece of its triggers that could be seen}.

    A piece is one occurrence (in root context) under a trigger component, with its
    own bodies as proxies in that context — so `isVisible` on them is what the
    viewport draws: the body's bulb, its bodies folder, every parent occurrence's
    bulb. Isolation is done with exactly those bulbs (Visibility Snapshot), so an
    isolated-away trigger reads as not visible.

    The OWNER's own subtree is left out of its triggers' pieces. A trigger that is an
    ancestor of the owner (Enclosure for Ribs inside it) otherwise always counts as
    visible whenever the owner is — isolating into Ribs shows only Ribs, which is
    still inside Enclosure. What matters is whether anything of the trigger OTHER
    than the owner is drawn.

    Proxies are cross-boundary objects and building them costs; they're cached on the
    rule set + the document token, and only their `isVisible` is read each tick.
    """
    key = (frozenset(rules.items()), _doc_token(design))
    hit = _parts_cache.get(doc_name)
    if hit and hit[0] == key:
        return hit[1]
    out = {}
    try:
        root = design.rootComponent
        comps = design.allComponents
    except Exception:
        return {}

    def walk(occ, own, acc):
        try:
            comp = occ.component
            cid = comp.id
        except Exception:
            return
        if cid in own:                 # the owner's subtree, all the way down
            return
        bodies = []
        try:
            for i in range(comp.bRepBodies.count):
                bodies.append(comp.bRepBodies.item(i).createForAssemblyContext(occ))
        except Exception:
            pass
        acc.append((occ, cid, bodies))
        try:
            for ch in occ.childOccurrences:
                walk(ch, own, acc)
        except Exception:
            pass

    for owner, ts in rules.items():
        own = subtrees.get(owner) or frozenset((owner,))
        acc = []
        for tid in ts:
            try:
                comp = comps.itemById(tid)
                if comp is None or comp == root:
                    continue
                for occ in root.allOccurrencesByComponent(comp):
                    walk(occ, own, acc)
            except Exception:
                continue
        out[owner] = acc
    _parts_cache[doc_name] = (key, out)
    return out


def _fired(design, doc_name, parts, hides, chain):
    """Owners with at least one trigger drawn right now (OR across triggers). A body
    the Hide Entirely policy is keeping off the screen counts as not drawn."""
    out = set()
    stale = False
    for owner, acc in parts.items():
        for occ, cid, bodies in acc:
            if cid in hides and cid not in chain:
                continue
            try:
                if not occ.isVisible:
                    continue
                if any(b.isVisible for b in bodies):
                    out.add(owner)
                    break
            except Exception:
                stale = True               # a proxy outlived its entity — rebuild
    if stale:
        _parts_cache.pop(doc_name, None)
    return frozenset(out)


def _sample_bodies(design, k=4):
    """Up to k bodies from distinct components, for _Native._validated."""
    out = []
    try:
        for comp in design.allComponents:
            try:
                if comp.bRepBodies.count:
                    out.append(comp.bRepBodies.item(0))
            except Exception:
                continue
            if len(out) >= k:
                break
    except Exception:
        pass
    return out


def apply(design, doc_name, force=False, edit_component=None):
    """Bring edge visibility in line with the policy. Cheap no-op when unchanged.

    `edit_component` is the component owning the sketch currently open for edit (the
    caller knows this; it needs the sketch-edit bookkeeping the visibility engine
    keeps). Passing None means "no sketch open", which is the pre-existing
    behaviour — so a caller that doesn't track it loses nothing else.
    """
    n = native()
    if not n.ok or design is None:
        return False

    flags = flagged_ids(doc_name)
    hides = hidden_ids(doc_name)
    rules = edge_triggers(doc_name)
    owners = flags | set(rules)
    managed = _managed.setdefault(doc_name, set())
    subtrees = _subtrees(design, doc_name, owners) if owners else {}
    covered = set().union(*subtrees.values()) if subtrees else set()
    targets = flags | hides | managed | covered

    # First apply for this document in this module instance: sweep EVERY component.
    #
    # _managed is the only record of what has been altered in the scene, and it lives in
    # memory — an add-in reload re-execs this file and it comes back empty, while the
    # scene still carries whatever edges were hidden. A component that was managed but is
    # no longer flagged then falls out of `targets` and is never visited again, so its
    # bodies stay edgeless permanently with nothing left that remembers why. Sweeping once
    # per load costs one pass and makes the policy self-healing: every component is
    # explicitly set to what the policy says it should be.
    if doc_name not in _swept:
        _swept.add(doc_name)
        force = True                       # never let the change-signature skip this pass
        try:
            targets = {c.id for c in design.allComponents}
        except Exception:
            pass

    if not targets:
        return False

    chain = _active_chain_ids(design, edit_component)
    # A flagged component is "entered" when anything in the chain lies in its subtree
    # — the active component, one of its ancestors, or the sketch owner. Testing the
    # subtree rather than `fid in chain` also covers a sketch edit, when the chain
    # loses the active component's ancestors (see _active_chain_ids).
    entered = frozenset(fid for fid in flags if chain & subtrees.get(fid, frozenset()))
    # Owners whose edges are off right now: a "not active" flag not entered, or a
    # visibility rule with any trigger drawn. Read every tick — a bulb flip moves
    # nothing the document token sees, so it has to be part of scan_key below.
    fired = _fired(design, doc_name, _trigger_parts(design, doc_name, rules, subtrees),
                   hides, chain) if rules else frozenset()
    hiders = frozenset((flags - entered) | fired)
    rules_key = frozenset(rules.items())
    # For each covered component, the owners whose subtree holds it; its edges show
    # only when none of them is hiding (a flagged child inside a flagged parent still
    # hides until you enter the child).
    covering = {}
    for fid, sub in subtrees.items():
        for cid in sub:
            covering.setdefault(cid, []).append(fid)

    # Body count is part of the signature so bodies added to a flagged component
    # later still get picked up — without it, an unchanged flag set plus unchanged
    # active component would short-circuit forever and the new body keep its edges.
    #
    # The scan below is O(components) of cross-boundary API calls, and the browser
    # sync calls apply() at ~7 Hz — on a large assembly that alone is thousands of
    # calls a second on the main thread, every one of which can fault in a
    # compressed page. Gate it behind the cheap document token, and rescan
    # unconditionally every _SCAN_MAX_GAP as the safety net. A pending retry always
    # rescans; its cadence is bounded by _RETRY_MIN_GAP further down.
    now = _time.time()
    scan_key = (frozenset(flags), frozenset(hides), rules_key, hiders,
                frozenset(chain & targets), _doc_token(design))
    _rs = _retry.get(doc_name)
    retry_due = bool(_rs) and (now - _rs[1]) >= _RETRY_MIN_GAP
    if (not force and not retry_due
            and _scan_sig.get(doc_name) == scan_key
            and (now - _scan_ts.get(doc_name, 0.0)) < _SCAN_MAX_GAP):
        return False
    _scan_sig[doc_name] = scan_key
    _scan_ts[doc_name] = now

    n_bodies = 0
    try:
        for comp in design.allComponents:
            try:
                if comp.id in targets:
                    n_bodies += comp.bRepBodies.count
            except Exception:
                continue
    except Exception:
        pass

    sig = (frozenset(flags), frozenset(hides), rules_key, hiders,
           frozenset(chain & targets), n_bodies)
    if not force and _last_sig.get(doc_name) == sig:
        return False

    # A previous attempt was incomplete: space the retries out and cap them, so a
    # design where some body never resolves can't turn into a 3 Hz native-call loop.
    state = _retry.get(doc_name)
    if state and not force:
        tries, last = state
        if tries >= _RETRY_MAX:
            _last_sig[doc_name] = sig      # give up and latch; edges stay as they are
            _retry.pop(doc_name, None)
            return False
        if _time.time() - last < _RETRY_MIN_GAP:
            return False

    # Discovery needs other bodies to cross-check a candidate path against; only
    # gathered when there is no path yet, so the steady state pays nothing.
    peers = _sample_bodies(design) if n.path is None else None

    touched = set()
    incomplete = False
    resolved = set()       # a repeat means the path isn't per-body — drop it
    try:
        for comp in design.allComponents:
            try:
                cid = comp.id
            except Exception:
                continue
            if cid not in targets:
                continue
            # "Inside" means the active component is this one or one of its
            # descendants, or the sketch being edited belongs to it. Anything not
            # set (or just un-set) resolves to fully visible, which is how
            # un-flagging restores a component. Edges also follow the owning
            # ANCESTORS: hidden while any owner above (or at) this one is hiding.
            # (Being inside needs no test of its own: it puts every flagged owner
            # above in `entered`, and a visibility rule deliberately ignores it.)
            inside = cid in chain
            if cid in hides and not inside:
                face_visible = False           # hide entirely: faces and edges
                edge_visible = False
            else:
                face_visible = True
                edge_visible = not any(h in hiders for h in covering.get(cid, ()))
            for i in range(comp.bRepBodies.count):
                try:
                    body = comp.bRepBodies.item(i)
                    ptr = n.resolve(body, peers)
                    if not ptr:
                        incomplete = True
                        continue
                    if ptr in resolved:
                        n.path = None
                        peers = _sample_bodies(design)
                        incomplete = True
                        continue
                    resolved.add(ptr)
                    node = n.edges_node(ptr)
                    if node:
                        n.set_node_visible(node, edge_visible)
                    else:
                        incomplete = True
                    fnode = n.faces_node(ptr)
                    if fnode:
                        n.set_node_visible(fnode, face_visible)
                    else:
                        incomplete = True
                except Exception:
                    incomplete = True
            if cid in covering or cid in hides:
                touched.add(cid)
    except Exception:
        return False

    _managed[doc_name] = touched
    # Only remember the signature once every body actually took the change. At
    # document-open the scene nodes may not exist yet; recording the signature then
    # would latch a half-applied state and never retry.
    if incomplete:
        _last_sig.pop(doc_name, None)
        tries = (_retry.get(doc_name) or [0, 0.0])[0]
        _retry[doc_name] = [tries + 1, _time.time()]
    else:
        _last_sig[doc_name] = sig
        _retry.pop(doc_name, None)
    return True


def invalidate(doc_name=None):
    """Force the next apply() to rewrite everything (new document, new bodies)."""
    if doc_name is None:
        _subtree_cache.clear()
        _parts_cache.clear()
        _last_sig.clear()
        _scan_sig.clear()
        _scan_ts.clear()
    else:
        _subtree_cache.pop(doc_name, None)
        _parts_cache.pop(doc_name, None)
        _last_sig.pop(doc_name, None)
        _scan_sig.pop(doc_name, None)
        _scan_ts.pop(doc_name, None)
