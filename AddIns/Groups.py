"""
Groups — the Browser window's groups, as a feature that follows one sees them.

Shared by Combine, Extrude and Mirror (loaded by path, like Phases.py): a run of
combines, an extrude's participant bodies or a mirror's bodies can be a whole Browser
GROUP — or a whole COMPONENT — rather than the bodies in it today, and stay in step as
bodies are filed in or out, made or deleted.

  A LIVE SET (Extrude, Mirror).  The record a follower keeps on itself — JSON {g: group
  ids, c: its component's id or '', fp: live_fingerprint as last synced, off: suppressed
  here}. live_sync brings one into step WHERE IT IS: it takes the group as it stands
  just before the follower, and never moves. A body that only exists after it (a later
  cut split one, a later feature made one) is left alone. Followers used to be pinned
  to the Dependent phase and moved below a later body's maker; an old pin is dropped
  (unpin). Combine's runs work the same way.

  WHAT A GROUP IS.  One of the Browser window's folders: fusion_browser/folders.json,
  which the window owns — read here, never written. A body is in the innermost group it
  is filed in; a group's bodies are its own and those of every group inside it. The
  group a follower offers or takes for a body is its TOP-LEVEL one (top, group_of). Bodies
  are filed by leaf key (Visibility Snapshot.py's _keyed), so that is how they are found
  again, wherever the timeline marker is.

  STAYING IN STEP.  fingerprint() is what a follower stores; Watcher calls its check a
  beat after each finished command and after folders.json moves (filing is the window's,
  not a command), on the main thread, once Fusion is idle.

  THE WARNING.  A follower names its groups in an attribute; the Browser add-in reads
  them (_group_uses) and the window asks before exploding one. A group exploded anyway
  leaves its followers as they are.
"""

import builtins
import json
import os
import threading
import time
import traceback
import types
import zlib

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface

_ADDINS_DIR = os.path.dirname(os.path.abspath(__file__))
FOLDERS = '/Users/josh/Code/Scripts/fusion_browser/folders.json'
_folders = {'mtime': None, 'byDoc': {}}
_fvs = None


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


def _vs():
    """Visibility Snapshot.py, loaded the first time a group is asked about."""
    global _fvs
    if _fvs is None:
        _fvs = _load('fvs', 'Visibility Snapshot.py')
    return _fvs


def _native(entity):
    try:
        return entity.nativeObject or entity
    except Exception:
        return entity


def _doc_groups(design):
    """(folders {id: def}, leaves {leaf key: folder id}) for this document."""
    try:
        st = os.stat(FOLDERS).st_mtime
    except OSError:
        return {}, {}
    if _folders['mtime'] != st:
        try:
            with open(FOLDERS, 'r', encoding='utf-8') as f:
                _folders['byDoc'] = (json.load(f) or {}).get('byDoc') or {}
            _folders['mtime'] = st
        except Exception:
            pass                           # caught mid-write: the last read stands
    d = _folders['byDoc'].get(_vs()._lineage(design)) or {}
    folders = {f['id']: f for f in d.get('folders') or [] if f and f.get('id')}
    return folders, d.get('leaves') or {}


def leaf_key(body):
    try:
        return _vs().leaf_key_for(_native(body))
    except Exception:
        return None


def top(folders, gid):
    """The outermost group holding `gid` (itself when top-level), or None when it is no
    group. What a follower offers and takes: the subgroups inside one — a big mirror's
    "Right" / "Left" — are filing, and by bare name two of them read the same."""
    if gid not in folders:
        return None
    seen = {gid}
    while folders[gid].get('parent') in folders and folders[gid]['parent'] not in seen:
        gid = folders[gid]['parent']
        seen.add(gid)
    return gid


def group_of(design, body):
    """The id of the top-level group a body is filed in (see top), or None."""
    folders, leaves = _doc_groups(design)
    return top(folders, leaves.get(leaf_key(body) or ''))


def group_name(design, gid):
    return (_doc_groups(design)[0].get(gid) or {}).get('name') or 'Group'


def exists(design, gid):
    return gid in _doc_groups(design)[0]


def member_keys(design, gid):
    """The leaf keys filed in a group or any group inside it; None when it is gone."""
    folders, leaves = _doc_groups(design)
    if gid not in folders:
        return None
    inside, grew = {gid}, True
    while grew:
        grew = False
        for fid, f in folders.items():
            if fid not in inside and f.get('parent') in inside:
                inside.add(fid)
                grew = True
    return {k for k, fid in leaves.items()
            if fid in inside and isinstance(k, str) and '|bd|' in k}


def keyed_bodies(comp):
    """[(leaf key, native solid body)] of a component's own bodies, in order."""
    try:
        cid = comp.id
        return [(k, b) for k, b in _vs()._keyed(cid, 'bd', comp.bRepBodies) if b.isSolid]
    except Exception:
        return []


def group_members(design, gid):
    """[(leaf key, native solid body)] of a group, or None when the group is gone."""
    keys = member_keys(design, gid)
    if keys is None:
        return None
    by_comp = {}
    for k in keys:
        by_comp.setdefault(k.split('|', 1)[0], set()).add(k)
    out = []
    for cid in sorted(by_comp):
        try:
            c = design.allComponents.itemById(cid) if cid else design.rootComponent
        except Exception:
            c = None
        if c is not None:
            out += [(k, b) for k, b in keyed_bodies(c) if k in by_comp[cid]]
    return out


def group_bodies(design, gid):
    m = group_members(design, gid)
    return None if m is None else [b for _, b in m]


def union(lists):
    """Bodies of several sets, each once."""
    out, seen = [], set()
    for bs in lists:
        for b in bs or []:
            try:
                t = _native(b).entityToken
            except Exception:
                t = repr(b)
            if t not in seen:
                seen.add(t)
                out.append(b)
    return out


def fingerprint(design, gids):
    """One number per group for who is in it now — filing a body in or out moves it, as
    does deleting one; -1 for a group that is gone."""
    out = []
    for gid in gids:
        m = group_members(design, gid)
        out.append(-1 if m is None
                   else zlib.crc32('\n'.join(sorted(k for k, _ in m)).encode()))
    return out


# ── A live set ───────────────────────────────────────────────────────────────

PHASE_ATTR, DEPENDENT = ('josh.phase', 'phase'), '5'


def _tok(b):
    try:
        return _native(b).entityToken
    except Exception:
        return repr(b)


def _component(design, cid):
    try:
        return design.allComponents.itemById(cid) if cid else None
    except Exception:
        return None


def live_fingerprint(design, rec):
    """fingerprint() of the record's groups, plus one for its component: who is in it."""
    out = fingerprint(design, rec.get('g') or [])
    if rec.get('c'):
        c = _component(design, rec['c'])
        out.append(-1 if c is None else
                   zlib.crc32('\n'.join(sorted(k for k, _ in keyed_bodies(c))).encode()))
    return out


def live_gone(design, rec):
    """Every group the record follows is gone (and its component, if it follows one)."""
    fp = live_fingerprint(design, rec)
    return bool(fp) and all(n == -1 for n in fp)


def live_keyed(design, rec, comp):
    """[(leaf key, native solid body)] of `comp` the record names, wherever the marker
    is: all of them for a component, else those filed in its groups."""
    if rec.get('c'):
        return keyed_bodies(comp)
    want = set()
    for g in rec.get('g') or []:
        want |= member_keys(design, g) or set()
    return [(k, b) for k, b in keyed_bodies(comp) if k in want]


def live_bodies(design, rec, comp):
    return [b for _, b in live_keyed(design, rec, comp)]


# ── Changed only after it ────────────────────────────────────────────────────
# A fingerprint is read at the END of the timeline, but a follower takes its set as it
# stands just before itself. So a body made AFTER the follower — the next thing you
# extrude in the component, a mirror's copies — moved the fingerprint, and every
# follower rolled back and recomputed the model (~2 s each, 7.5 s for a run of 13
# combines) to apply the very set it already had: ~15 s of beachball after each
# extrude or delete in Ribs on 260612 (2026-10-08). So a sync also records the keys it
# took THERE ('at') and what stood at the end ('end'), and a moved fingerprint is
# first explained: every key gone since was never one it took, every key new since is
# a body some feature after the follower made. Anything unexplained syncs as before.

_ok = builtins.__dict__.setdefault('_groups_in_step', {})   # (where, rec) -> fp proven


def _ok_key(feat, rec):
    try:
        return (feat.parentComponent.id, feat.name,
                json.dumps({k: rec.get(k) for k in ('g', 'c', 'tg', 'og', 'tc', 'oc')},
                           sort_keys=True))
    except Exception:
        return None


def made_after(design, feat, bodies):
    """Whether every body in `bodies` (native) is made by a feature AFTER `feat` and
    none before it — `Feature.bodies` lists what a feature made or changed, so a body
    any earlier feature lists already stood there. One that no feature lists (a base
    feature, a paste) is not proven: False."""
    if not bodies:
        return True
    try:
        idx = feat.timelineObject.index
        comps = {_native(b).parentComponent.id for b in bodies}
        items = _timeline_bodies(design)
    except Exception:
        return False
    left = [_native(b) for b in bodies]
    made_by = _scan['made']
    for i, cid, f in items:
        if i == idx or cid not in comps:
            continue
        if i not in made_by:
            try:
                made_by[i] = [_native(b) for b in f.bodies]
            except Exception:
                made_by[i] = []
        for b in list(left):
            if any(m == b for m in made_by[i]):
                if i < idx:
                    return False                  # it stood before the follower
                left.remove(b)
        if not left and i > idx:
            break
    return not left


# Shared by every copy of this module (Extrude, Mirror, Combine each load one).
_scan = builtins.__dict__.setdefault('_groups_scan',
                                     {'key': None, 'at': 0.0, 'items': None, 'made': {}})


def _timeline_bodies(design):
    """[(timeline index, component id, feature)] for every feature, in order; what each
    made or changed is read lazily into _scan['made'] (only for the components asked
    about). One walk serves every follower the watchers check after a command — kept
    3 s, keyed on the timeline's length (a sync in between recomputes, and a body read
    before it then compares unequal: made_after says False, the safe answer)."""
    tl = design.timeline
    key = (id(design), tl.count, tl.markerPosition)
    now = time.monotonic()
    if _scan['key'] == key and now - _scan['at'] < 3.0:
        return _scan['items']
    items = []
    for i in range(tl.count):
        try:
            f = adsk.fusion.Feature.cast(tl.item(i).entity)
            if f is not None:
                items.append((i, f.parentComponent.id, f))
        except Exception:
            continue
    _scan.update(key=key, at=now, items=items, made={})
    return items


def changed_after(design, feat, rec, now):
    """True when the record's set where `feat` sits is provably what its last sync
    took, though the fingerprint at the end moved: `now` is [(key, body)] the record
    names at the end of the timeline now. Needs 'at' / 'end' from a sync (live_sync,
    or the caller's own)."""
    if 'at' not in rec or 'end' not in rec:
        return False
    at, end = set(rec['at']), set(rec['end'])
    cur = dict(now)
    gone = end - set(cur)
    new = set(cur) - end
    if gone & at or new & at:
        return False
    return made_after(design, feat, [cur[k] for k in new])


def in_step(design, feat, rec, fp=None, now=None):
    """The follower needs no sync: its fingerprint is the stored one, or the change is
    one changed_after explains (remembered for this fingerprint, in memory — writing it
    on the feature would be an undo step of its own). `fp`: the fingerprint now (default
    live_fingerprint); `now`: the [(key, body)] it names at the end (default
    live_keyed in the feature's component)."""
    if fp is None:
        fp = live_fingerprint(design, rec)
    if fp == rec.get('fp'):
        return True
    key = _ok_key(feat, rec)
    if key is not None and _ok.get(key) == (json.dumps(fp), rec.get('fp')):
        return True
    try:
        if now is None:
            now = live_keyed(design, rec, feat.parentComponent)
        ok = changed_after(design, feat, rec, now)
    except Exception:
        ok = False
    if ok and key is not None:
        _ok[key] = (json.dumps(fp), rec.get('fp'))
    return ok


def unpin(feat, step):
    """Drop the Dependent pin an older follower carries (Phases.py's override), so later
    features stay below it. An attribute write is an undo step: `step` (the caller's
    tickguard.one_step(...), entered only when there is a pin). Best-effort."""
    try:
        a = feat.attributes.itemByName(*PHASE_ATTR)
        if a is not None and a.value == DEPENDENT:
            with step:
                a.deleteMe()
    except Exception:
        pass


def live_sync(design, feat, rec, apply, log):
    """Bring one follower into step, marker at the end in and out (the caller holds the
    undo step). Rolled to just before it, `apply(feat, bodies)` gets the set THERE. None left:
    suppressed (rec['off']) until there are some again. Updates rec's fp / off; returns
    how many bodies it now takes."""
    tl = design.timeline
    comp = feat.parentComponent
    if feat.timelineObject.isSuppressed:
        feat.timelineObject.isSuppressed = False
    parts = []
    try:
        feat.timelineObject.rollTo(True)
        there = live_keyed(design, rec, comp)
        parts = [b for _, b in there]
        rec['at'] = sorted(k for k, _ in there)
        if parts:
            apply(feat, parts)
        tl.moveToEnd()
        if not parts:
            feat.timelineObject.isSuppressed = True    # empty would mean ALL, or nothing
        rec['off'] = not parts
    finally:
        if tl.markerPosition != tl.count:
            tl.moveToEnd()
    rec['fp'] = live_fingerprint(design, rec)
    rec['end'] = sorted(k for k, _ in live_keyed(design, rec, comp))
    return len(parts)


# ── The watcher ──────────────────────────────────────────────────────────────

def _handler(base, fn, log):
    class H(base):
        def __init__(self):
            super().__init__()

        def notify(self, args):
            try:
                fn(args)
            except Exception:
                log('handler failed:', traceback.format_exc().replace('\n', ' | '))
    return H()


class Watcher:
    """Runs check(design) a beat after each finished command — after the Timeline add-in
    has placed whatever the command made — and after folders.json moves; main thread,
    only once Fusion is idle (`busy()` False) with the marker at the end. Not after Undo
    or Redo: re-syncing those would undo the undo."""
    DELAY = 1.0
    SKIP = frozenset(('UndoCommand', 'RedoCommand'))

    def __init__(self, event, check, log, busy=lambda: False):
        self.event, self.check, self.log, self.busy = event, check, log, busy
        self.keep = []
        self.custom = None
        self.due = 0.0
        self.tries = 0
        self.stop_evt = None

    def start(self):
        try:
            app.unregisterCustomEvent(self.event)
        except Exception:
            pass
        self.custom = app.registerCustomEvent(self.event)
        h = _handler(adsk.core.CustomEventHandler, self._fire, self.log)
        self.custom.add(h)
        self.keep.append(h)
        t = _handler(adsk.core.ApplicationCommandEventHandler, self._terminated, self.log)
        ui.commandTerminated.add(t)
        self.keep.append(t)
        self.stop_evt = threading.Event()
        threading.Thread(target=self._clock, args=(self.stop_evt,), daemon=True).start()
        self.due = time.monotonic() + self.DELAY       # a document opened with work to do

    def stop(self):
        if self.stop_evt is not None:
            self.stop_evt.set()
        for h in self.keep:
            for evt in (self.custom, ui.commandTerminated):
                try:
                    if evt is not None:
                        evt.remove(h)
                except Exception:
                    pass
        try:
            app.unregisterCustomEvent(self.event)
        except Exception:
            pass
        self.custom = None
        del self.keep[:]

    def _terminated(self, args):
        # Any ending but Undo / Redo: a pre-empted command is often executed, and the
        # check is a fingerprint read per follower when nothing changed.
        if args.commandId in self.SKIP:
            return
        self.tries = 0
        self.due = time.monotonic() + self.DELAY

    def _clock(self, stop):
        seen = None
        while not stop.is_set():
            try:
                st = os.stat(FOLDERS).st_mtime
            except OSError:
                st = None
            if st != seen:
                if seen is not None and not self.due:
                    self.tries = 0
                    self.due = time.monotonic() + self.DELAY
                seen = st
            if self.due and time.monotonic() >= self.due:
                self.due = 0.0
                app.fireCustomEvent(self.event, '')
            stop.wait(0.25)

    def _fire(self, args):
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None or design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            return
        if ui.activeCommand != 'SelectCommand' or self.busy():
            self.tries += 1
            if self.tries < 40:
                self.due = time.monotonic() + 0.5          # try again once it is idle
            return
        tl = design.timeline
        if tl.markerPosition != tl.count:
            return                                         # rolled back: leave it be
        self.check(design)
