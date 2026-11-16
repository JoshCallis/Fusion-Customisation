"""
Combine — the native feature, wearing the value window's face.

Mirror's arrangement (read Mirror.py), through the shared engine in ../Feature Panel.py:
a command of our own runs with its palette hidden, every change rebuilds a real
CombineFeature in executePreview, and the value window's feature panel steers it. Only
what is different is said here.

  CLICK THE TARGET, THEN THE TOOLS.  The first body clicked is the target; every body
  clicked after it is a tool. Started with bodies selected, the first of them is the
  target and the rest are tools. "Pick" on the Target row lets go of it and takes the
  next body clicked instead. Once picked, the target can't be clicked at all (the
  engine's selectable()), so a tool sitting flush on it is what a click on the shared
  face finds.

  TOOLS FROM ANY COMPONENT.  The feature is made in the target's component. A tool from
  another component goes in as it was clicked (its proxy), as Fusion's own Combine takes
  it — the cross-component link Extrude's docstring avoids, made here on purpose. Such
  a combine is tagged (ALSO_IN: the other components' ids) so the Timeline add-in lists
  it in the Browser under each of them too — its tool may be consumed, and then the
  feature's own bodies no longer say where it reached.

  JOIN BY DEFAULT, TOOLS NOT KEPT.  Fusion's defaults. New Component is left out: a new
  component made by a combine is made outside the bounds scheme.

  A COMPONENT AS THE TARGET, OR AS THE TOOL.  "Target is: Component" makes the target
  every solid body in the clicked target's component (its own bodies — not those of
  components inside it); "Tools are: Component" makes the tools every solid body in
  the components of the clicked tools. Started with components selected (the Browser),
  the first is the target component and the rest are tool components. A Combine takes
  ONE target, so a component target is a run of combines, one per body — the tools
  kept by all but the last — or, for Join, one combine that joins the component's other
  bodies in with the tools. A component target lights all its bodies in the viewport.

  A BROWSER GROUP, THE SAME WAY.  "Group" on either row takes the group the clicked body
  is filed in (the innermost; with the groups inside it) — see Browser groups below —
  and a chips row under it lists the groups in that component (the target's; for the
  tools, the clicked tools' too) to choose from instead, as Extrude's does. A target
  chip makes one of that group's bodies the target; tool chips toggle, and a tool
  clicked puts its group's chip on (once, so a chip turned off stays off). It
  stays live like a component (LINK's tg / og), following bodies filed in or out; the
  Browser window warns before exploding a group a run follows. A group exploded anyway
  leaves its runs as they are.

  A COMPONENT STAYS LIVE (the section of that name below).  Each combine of such a run
  carries LINK (the components, and the run it belongs to). After every command that
  changes what is in a linked component, the run is rebuilt WHERE IT IS in the
  timeline, from what the component / group holds there: a new body is taken in (a new
  target gets its own combine), a deleted one is let go, and with nothing left to work
  on the run is suppressed until there is. A body that only exists after the run (a
  later cut split one, a later feature made one) is left alone — the run never moves,
  and isn't pinned to a phase (an old run's Dependent pin is dropped: _unpin).

NOT HERE (YET): editing an existing combine still opens Fusion's own dialog.
"""

import json
import os
import threading
import time
import traceback
import types
import uuid

import adsk.core
import adsk.fusion

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fp = _load('fpanel', 'Feature Panel.py')
tickguard = _load('tickguard', 'Tick Guard.py')

app = adsk.core.Application.get()
ui = app.userInterface

_OPERATIONS = {
    'join': adsk.fusion.FeatureOperations.JoinFeatureOperation,
    'cut': adsk.fusion.FeatureOperations.CutFeatureOperation,
    'intersect': adsk.fusion.FeatureOperations.IntersectFeatureOperation,
}

# The tag on a cross-component combine: JSON list of the OTHER components' ids. Read by
# Timeline.py's _feat_rows (same group and name, spelled there too).
ATTR_GROUP = 'ScriptsCombine'
ALSO_IN = 'alsoIn'
# The live link on each combine of a component run — JSON:
#   g     the run's id (every combine of one run shares it)
#   op    'join' | 'cut' | 'intersect';  keep  'keep' | 'remove' (the run's last combine)
#   tc    the target component's id, or '' (the target is one body: tb)
#   oc    the tool components' ids, or [] (the tools are the bodies tt)
#   tg    the target Browser group's id, or ''  (in place of tc)
#   og    the tool Browser groups' ids, or []   (in place of oc) — read by the Browser
#         window too, which warns before a group a run follows is exploded
#   tb    THIS combine's target body (native entityToken)
#   tt    the tool bodies' tokens, when oc is empty
#   fp    fingerprint() as last synced — a command that leaves it alone changed nothing
LINK = 'link'
# Phases.py's override: the Dependent phase. Runs used to be pinned there (every
# body-making feature placed above them); now only dropped from old runs (_unpin).
PHASE_GROUP, PHASE_NAME, DEPENDENT = 'josh.phase', 'phase', '5'


def _log(*parts):
    try:
        with open('/tmp/fusion_combine.log', 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _tb():
    return traceback.format_exc().replace('\n', ' | ')


# ── Bodies, components, contexts ─────────────────────────────────────────────

def solid_bodies(comp):
    """A component's own solid bodies, as native bodies."""
    out = []
    try:
        bs = comp.bRepBodies
        for i in range(bs.count):
            b = bs.item(i)
            if b.isSolid:
                out.append(b)
    except Exception:
        pass
    return out


def _cid(entity):
    try:
        return entity.parentComponent.id
    except Exception:
        return None


# ── Browser groups ───────────────────────────────────────────────────────────
# Groups.py (shared with Extrude): what a group is, who is in it, and the watcher.

groups = _load('groups', 'Groups.py')
group_of = groups.group_of
group_name = groups.group_name
group_members = groups.group_members
group_bodies = groups.group_bodies
_union = groups.union


def _tok(body):
    return fp.token(fp.native(body))


def _find(design, tok):
    """The native body a stored token names now, or None."""
    if not tok:
        return None
    try:
        for e in design.findEntityByToken(tok):
            b = adsk.fusion.BRepBody.cast(e)
            if b is not None:
                return fp.native(b)
    except Exception:
        pass
    return None


def _occ_of(design, comp, hints):
    """The occurrence a body of `comp` is seen through: the one it was clicked in, else
    the component's first. None for the root."""
    if comp is None or comp == design.rootComponent:
        return None
    occ = hints.get(comp.id) if hints else None
    if occ is not None:
        return occ
    try:
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        return occs.item(0) if occs.count else None
    except Exception:
        return None


def proxy(design, body, hints=None):
    """`body` as seen from the root — a proxy, or the native body of a root body."""
    b = fp.native(body)
    occ = _occ_of(design, b.parentComponent, hints)
    if occ is None:
        return b
    try:
        return b.createForAssemblyContext(occ)
    except Exception:
        return b


def plan(op, targets, tools, keep):
    """The combines a run is: [(target, tools, keep tools)]. One per target — the tools
    kept by all but the last, or the last would take them from the next — except Join,
    which is ONE combine joining the other targets in with the tools."""
    if not targets or not tools:
        return []
    if op == 'join':
        return [(targets[0], list(targets[1:]) + list(tools), keep)]
    n = len(targets)
    return [(t, list(tools), keep if i == n - 1 else True) for i, t in enumerate(targets)]


def create(design, target, tools, op, keep, hints=None, log=_log):
    """One CombineFeature, made in the target's component. All in one component: the
    native bodies, as ever. Across components: the tools as proxies, first with the
    native target in its own component, as the Combines already in the tree were made,
    then all as proxies in the root."""
    target = fp.native(target)
    own = target.parentComponent
    cross = any(_cid(fp.native(t)) != own.id for t in tools)
    tries = [(own, target, (lambda b: fp.native(b)) if not cross
              else (lambda b: proxy(design, b, hints)))]
    if cross:
        tries.append((design.rootComponent, proxy(design, target, hints),
                      lambda b: proxy(design, b, hints)))
    for comp, tgt, ctx in tries:
        coll = adsk.core.ObjectCollection.create()
        for t in tools:
            coll.add(ctx(t))
        feats = comp.features.combineFeatures
        try:
            inp = feats.createInput(tgt, coll)
            inp.operation = _OPERATIONS.get(op, _OPERATIONS['join'])
            inp.isKeepToolBodies = bool(keep)
            return feats.add(inp)
        except Exception:
            log('build in', comp.name, 'failed:', _tb())
    return None


def _in_context_of(design, feat):
    """How a body goes into an existing combine: native in its own component, else as
    a proxy (as create() made it)."""
    own = feat.parentComponent

    def ctx(b):
        b = fp.native(b)
        if own != design.rootComponent and _cid(b) == own.id:
            return b
        return proxy(design, b)
    return ctx


def also_in(target, tools):
    own, out = _cid(fp.native(target)), []
    for t in tools:
        cid = _cid(fp.native(t))
        if cid and cid != own and cid not in out:
            out.append(cid)
    return out


def tag(feat, link, target, tools):
    """Write a combine's attributes: ALSO_IN, and for a run its LINK.
    The bodies must be valid where the marker is now."""
    others = also_in(target, tools)
    if others:
        feat.attributes.add(ATTR_GROUP, ALSO_IN, json.dumps(others))
    if link is not None:
        mine = dict(link)
        mine['tb'] = _tok(target)
        feat.attributes.add(ATTR_GROUP, LINK, json.dumps(mine))


def fingerprint(design, feats, link):
    """Cheap, read at the end of the timeline: how many bodies each linked component
    has, and each combine's health. Whatever adds or deletes a body there moves a count
    — or, when the tools were consumed, breaks a combine."""
    parts = []
    comps = design.allComponents
    for cid in [link.get('tc')] + list(link.get('oc') or []):
        if not cid:
            continue
        c = comps.itemById(cid)
        parts.append(c.bRepBodies.count if c is not None else -1)
    parts += groups.fingerprint(
        design, [g for g in [link.get('tg')] + list(link.get('og') or []) if g])
    for f in feats:
        try:
            parts.append([int(f.healthState), bool(f.timelineObject.isSuppressed)])
        except Exception:
            parts.append(None)
    return json.dumps(parts)


def _set_fp(design, feats, at=None):
    """Store the fingerprint on every combine of a run (each holds its own LINK) — and,
    after a sync, the keys the run took where it sits (`at`) and what its components
    and groups hold at the end, for Groups.in_step."""
    links = [_link_of(f) for f in feats]
    if not feats or links[0] is None:
        return
    now = fingerprint(design, feats, links[0])
    end = sorted(k for k, _ in _linked_keyed(design, links[0])) if at is not None else None
    for f, l in zip(feats, links):
        if l is None:
            continue
        if l.get('fp') != now or (at is not None and (l.get('at'), l.get('end')) != (at, end)):
            l['fp'] = now
            if at is not None:
                l['at'], l['end'] = at, end
            f.attributes.add(ATTR_GROUP, LINK, json.dumps(l))


def _linked_keyed(design, link):
    """[(leaf key, native body)] of everything the run's linked components and groups
    hold where the marker is."""
    out = {}
    comps = design.allComponents
    for cid in [link.get('tc')] + list(link.get('oc') or []):
        c = comps.itemById(cid) if cid else None
        if c is not None:
            for k, b in groups.keyed_bodies(c):
                out.setdefault(k, b)
    for g in [link.get('tg')] + list(link.get('og') or []):
        for k, b in (group_members(design, g) or []) if g else []:
            out.setdefault(k, b)
    return list(out.items())


def _later_only(design, feats, link, fp_now):
    """The run's fingerprint moved only by bodies made after it (Groups.in_step) — the
    combines' health unchanged, and a run that follows components or groups."""
    try:
        now, was = json.loads(fp_now), json.loads(link.get('fp') or 'null')
    except Exception:
        return False
    n = len(feats)
    if not isinstance(was, list) or len(was) != len(now) or now[-n:] != was[-n:]:
        return False
    if not (link.get('tc') or link.get('oc') or link.get('tg') or link.get('og')):
        return False
    return groups.in_step(design, feats[0], link, fp=fp_now,
                          now=_linked_keyed(design, link))


def _link_of(feat):
    try:
        a = feat.attributes.itemByName(ATTR_GROUP, LINK)
        return json.loads(a.value) if a is not None else None
    except Exception:
        return None


# ── A component stays live ───────────────────────────────────────────────────
# A Combine holds bodies, never a component, so the run is kept in step by hand. Fusion
# says nothing when a body is added or deleted, so after each finished command (not an
# Undo / Redo — re-syncing those would undo the undo) the runs' fingerprints are read
# at the end of the timeline; a run whose fingerprint moved is rebuilt in one undo step:
#
#   1. Rolled to just before the run, what is in each linked component THERE is what
#      the run takes. A body that only exists after the run is not the run's: it
#      stays where it is.
#   2. A combine whose target is still wanted keeps it; one whose target went takes a
#      new target; surplus combines are deleted, missing ones made after the last.
#   3. Every combine gets the tools (and the run's keep-the-tools rule) again.
#   4. With no target or no tool left, the run is suppressed — and comes back once
#      there is something to combine again.

def runs(design):
    """{run id: [(combine, link)]} in timeline order."""
    out = {}
    try:
        attrs = design.findAttributes(ATTR_GROUP, LINK)
    except Exception:
        return out
    for a in attrs:
        try:
            f = adsk.fusion.CombineFeature.cast(a.parent)
            if f is None or not f.isValid:
                continue
            link = json.loads(a.value)
            out.setdefault(link.get('g') or '', []).append((f, link))
        except Exception:
            continue
    for g in out:
        out[g].sort(key=lambda fl: fl[0].timelineObject.index)
    return out


_synced_at = [None]     # the keys sync() found just before the run (check -> _set_fp)


def sync(design, feats, links, log=_log):
    """Rebuild one run in place (see above). Marker at the end, in and out."""
    tl = design.timeline
    link = links[0]
    comps = design.allComponents
    tc = comps.itemById(link['tc']) if link.get('tc') else None
    ocs = [c for c in (comps.itemById(i) for i in link.get('oc') or []) if c is not None]
    if (link.get('tc') and tc is None) or (link.get('oc') and not ocs):
        log('sync: a linked component is gone — run', link.get('g'), 'left as it is')
        return feats
    tg = link.get('tg') or ''
    ogs = [g for g in link.get('og') or [] if group_members(design, g) is not None]
    if (tg and group_members(design, tg) is None) or (link.get('og') and not ogs):
        log('sync: a linked group is gone — run', link.get('g'), 'left as it is')
        return feats

    # The target's and the tools' whole sets, read wherever the marker is — None for a
    # side that is bodies, not a set.
    def target_set():
        if tc is not None:
            return solid_bodies(tc)
        return (group_bodies(design, tg) or []) if tg else None

    def tool_set():
        if ocs:
            return [b for c in ocs for b in solid_bodies(c)]
        return _union(group_bodies(design, g) for g in ogs) if ogs else None

    def linked():
        return (target_set() or []) + (tool_set() or [])
    op = link.get('op') or 'join'
    keep = link.get('keep') == 'keep'

    # A suppressed combine can't be read or edited rolled back: bring back the ones
    # suppressed HERE (step 4). One suppressed by hand — or by Fusion, which suppresses
    # a combine whose tools' features were suppressed — stays so: unsuppressing it
    # would unsuppress those features too.
    if any(f.timelineObject.isSuppressed for f in feats):
        if not all(l.get('off') for l in links):
            log('sync: run', link.get('g'), 'is suppressed — left as it is')
            return feats
        for f in feats:
            f.timelineObject.isSuppressed = False

    # 1. What is there, just before the run.
    feats[0].timelineObject.rollTo(True)
    _synced_at[0] = sorted(k for k, _ in _linked_keyed(design, link))

    targets = target_set()
    if targets is None:
        t = _find(design, link.get('tb'))
        targets = [t] if t is not None else []
    tools = tool_set()
    if tools is None:
        tools = [b for b in (_find(design, t) for t in link.get('tt') or []) if b is not None]
    if (tc is not None or tg) and not (ocs or ogs):
        tt = {_tok(b) for b in tools}
        targets = [b for b in targets if _tok(b) not in tt]
    else:
        tgt = {_tok(b) for b in targets}
        tools = [b for b in tools if _tok(b) not in tgt]

    # 4. Nothing to combine: suppress the run, keep the link.
    if not targets or not tools:
        tl.moveToEnd()
        for f, l in zip(feats, links):
            f.timelineObject.isSuppressed = True
            l['off'] = True
            f.attributes.add(ATTR_GROUP, LINK, json.dumps(l))
        log('sync: run', link.get('g'), 'suppressed —',
            'no target' if not targets else 'no tool')
        return feats

    # Tokens from here on: every roll below recomputes, and a body read before it is
    # no longer valid after it.
    want = [_tok(b) for b in targets]
    tool_toks = [_tok(b) for b in tools]
    n_targets, n_tools = len(targets), len(tools)
    if op == 'join':                       # Join keeps the target it had, if it can
        cur = links[0].get('tb')
        if cur in want:
            want.insert(0, want.pop(want.index(cur)))
    steps = plan(op, want, tool_toks, keep)
    by_tok = {s[0]: s for s in steps}

    # 2. Match the combines to the targets.
    assign, used, free = {}, set(), []
    for i, l in enumerate(links):
        t = l.get('tb')
        if t in by_tok and t not in used:
            assign[i] = t
            used.add(t)
        else:
            free.append(i)
    news = [s[0] for s in steps if s[0] not in used]
    doomed = []
    for i in free:
        if news:
            assign[i] = news.pop(0)
        else:
            doomed.append(i)

    if doomed:
        tl.moveToEnd()
        for i in reversed(doomed):
            name = feats[i].name
            ok = False
            try:
                ok = feats[i].deleteMe()
            except Exception:
                pass
            log('sync: deleted' if ok else 'sync: could not delete', name)
    kept = [i for i in range(len(feats)) if i in assign]
    total = len(kept) + len(news)

    def resolve(step):
        tgt = _find(design, step[0])
        tls = [b for b in (_find(design, t) for t in step[1]) if b is not None]
        return tgt, tls

    # 3. Every combine its target, tools and keep rule — each read where it sits.
    base = {k: v for k, v in link.items() if k not in ('tb', 'fp', 'off')}
    done = []
    for k, i in enumerate(kept):
        f = feats[i]
        f.timelineObject.rollTo(True)
        tgt, tls = resolve(by_tok[assign[i]])
        if tgt is None or not tls:
            log('sync: lost a body rolling to', f.name)
            continue
        ctx = _in_context_of(design, f)
        try:
            same = _tok(f.targetBody) == assign[i]
        except Exception:
            same = False
        if not same:
            f.targetBody = ctx(tgt)
        coll = adsk.core.ObjectCollection.create()
        for b in tls:
            coll.add(ctx(b))
        f.toolBodies = coll
        f.isKeepToolBodies = keep if k == total - 1 else True
        tag(f, base, tgt, tls)
        done.append(f)
    if news and done:
        done[-1].timelineObject.rollTo(False)
        for k, t in enumerate(news):
            tgt, tls = resolve(by_tok[t])
            f = None
            if tgt is not None and tls:
                f = create(design, tgt, tls, op,
                           keep if len(kept) + k == total - 1 else True, log=log)
            if f is None:
                log('sync: could not make a combine for a new target')
                continue
            tag(f, base, tgt, tls)
            done.append(f)
            log('sync: made', f.name)
    tl.moveToEnd()
    log('sync: run', link.get('g'), '->', len(done), 'combine(s),',
        n_targets, 'target(s),', n_tools, 'tool(s)')
    return done


def _unpin(feats, log=_log):
    """Drop the Dependent pin an older run carries, so later features stay below it. A
    write is an undo step: one step, and only when there is one."""
    pinned = []
    for f in feats:
        try:
            a = f.attributes.itemByName(PHASE_GROUP, PHASE_NAME)
            if a is not None and a.value == DEPENDENT:
                pinned.append(a)
        except Exception:
            pass
    if not pinned:
        return
    try:
        with tickguard.one_step('CombineUnpin'):
            for a in pinned:
                a.deleteMe()
        log('unpinned', len(pinned), 'combine(s)')
    except Exception:
        log('unpin failed:', _tb())


def check(design, log=_log):
    """Every run whose fingerprint moved, synced. Main thread, idle Fusion only."""
    tl = design.timeline
    for g, items in runs(design).items():
        if tl.markerPosition != tl.count:
            return
        feats = [f for f, _ in items]
        links = [l for _, l in items]
        _unpin(feats, log)
        fp_now = fingerprint(design, feats, links[0])
        if fp_now == links[0].get('fp') or _later_only(design, feats, links[0], fp_now):
            continue
        t0 = time.perf_counter()
        try:
            with tickguard.hold('combine/sync'), tickguard.one_step('CombineSync'):
                _synced_at[0] = None
                try:
                    done = sync(design, feats, links, log)
                finally:
                    if tl.markerPosition != tl.count:
                        tl.moveToEnd()
                _set_fp(design, done, at=_synced_at[0])
        except Exception:
            log('sync failed:', _tb())
        log('sync took %.0f ms' % ((time.perf_counter() - t0) * 1000))


def _busy():
    return tickguard.busy() or tickguard._in_sketch()


def Live():
    """check() a beat after each finished command and each filing in the Browser."""
    return groups.Watcher('CombineLiveSync', check, _log, _busy)


# ── The command ──────────────────────────────────────────────────────────────

class Combine(fp.Feature):
    kind = 'combine'
    cmd_id = 'SmartCombineCmd'
    name = 'Combine'
    owner = 'Combine'
    desc = ('Join, cut or intersect bodies: the first one clicked is the target, the rest '
            'are tools, from any component. Either can be a whole Browser group or component.')
    object_filters = ('SolidBodies',)
    slots = {'target': ('SolidBodies',)}
    words = ('tool', 'tools')
    cycles = {'operation': 'operation'}
    defaults = {
        'operation': 'join',        # 'join' | 'cut' | 'intersect'
        'keep': 'remove',           # 'remove' | 'keep' — the tool bodies afterwards
        'targetScope': 'body',      # 'body' | 'group' | 'component' — the target's whole
        'toolScope': 'bodies',      # 'bodies' | 'group' | 'component' — the tools' whole
        'targetGroup': '',          # "Target is: Group": the group chosen on its chips
        'toolGroups': [],           # "Tools are: Group": the groups chosen (never mutated)
    }
    rows = [
        {'type': 'sel', 'key': 'target', 'label': 'Target', 'button': 'Pick'},
        {'type': 'seg', 'key': 'targetScope', 'label': 'Target is',
         'options': [['body', 'Body'], ['group', 'Group'], ['component', 'Component']]},
        {'type': 'chips', 'key': 'targetGroup', 'label': 'Group', 'single': True,
         'when': {'targetScope': ['group']}},
        {'type': 'sel', 'key': 'selection', 'label': 'Tools', 'button': 'Clear'},
        {'type': 'seg', 'key': 'toolScope', 'label': 'Tools are',
         'options': [['bodies', 'Bodies'], ['group', 'Group'], ['component', 'Component']]},
        {'type': 'chips', 'key': 'toolGroups', 'label': 'Groups',
         'when': {'toolScope': ['group']}},
        {'type': 'seg', 'key': 'operation', 'label': 'Operation',
         'options': [['cut', 'Cut'], ['intersect', 'Intersect'], ['join', 'Join']]},
        {'type': 'seg', 'key': 'keep', 'label': 'Tools after',
         'options': [['remove', 'Removed'], ['keep', 'Kept']]},
    ]

    def _reset(self):
        super()._reset()
        self.clicked = {}            # native body's token -> the body as clicked (proxy)
        self.tool_occs = []          # components pre-selected as tools (Browser rows)
        self.built = []              # [(combine, target, tools)] from the last build
        self.why = ''
        self.counts = None           # (targets, tools) as members() last found them
        self.lit = ()                # tokens of the bodies light() lit (self.shown)
        self.sets = (None, [])       # (target group, tool groups) as members() found them
        self.seen_tools = set()      # tools whose group has gone into toolGroups
        self.shown = ([], [])        # (target-side, tool-group) bodies light() shows — members()
        self.lit_sel = ()            # the selection light() left
        self.lit_tools = set()       # native tokens of the tool-group bodies it lit
        self.relighting = False

    def accept(self, entity):
        b = fp.body_of(entity)
        if b is None or not b.isSolid:
            return None
        as_clicked = adsk.fusion.BRepBody.cast(entity) or getattr(entity, 'body', None)
        if as_clicked is not None:
            self.clicked[fp.token(b)] = as_clicked
        return b

    def accept_pick(self, slot, entity):
        return self.accept(entity)

    def pick_label(self, slot, entity):
        try:
            return entity.name
        except Exception:
            return 'Picked'

    def narrow(self, objs):
        target = self.picks.get('target')
        if target is None:
            return objs
        return [o for o in objs if fp.token(o) != fp.token(target)]

    # What the target and tools are, components resolved.

    def target_comp(self):
        t = self.picks.get('target')
        if t is None:
            return None
        c = adsk.fusion.Component.cast(t)
        return c if c is not None else t.parentComponent

    def target_is_comp(self):
        return (self.spec.get('targetScope') == 'component'
                or adsk.fusion.Component.cast(self.picks.get('target')) is not None)

    def _design(self):
        return adsk.fusion.Design.cast(app.activeProduct)

    def target_group(self):
        """The target's group, with "Target is: Group": the one chosen on its chips, else
        the clicked target body's."""
        t = adsk.fusion.BRepBody.cast(self.picks.get('target'))
        if t is None or self.target_is_comp() or self.spec.get('targetScope') != 'group':
            return None
        design = self._design()
        g = self.spec.get('targetGroup')
        return g if g and groups.exists(design, g) else group_of(design, t)

    def seed_tool_groups(self):
        """With "Tools are: Group", each tool clicked puts its group on the chips (once —
        a chip turned off stays off while that body stays clicked)."""
        if self.spec.get('toolScope') != 'group':
            return
        design = self._design()
        chosen = list(self.spec.get('toolGroups') or [])
        for o in self.objects:
            k = fp.token(o)
            if k in self.seen_tools:
                continue
            self.seen_tools.add(k)
            g = group_of(design, o)
            if g is not None and g not in chosen:
                chosen.append(g)
        self.spec['toolGroups'] = chosen

    def tool_groups(self):
        """([group ids], [clicked bodies in no group]) of the tools, with "Tools are:
        Group" — the groups on its chips."""
        if self.spec.get('toolScope') != 'group':
            return [], []
        design = self._design()
        out = [g for g in self.spec.get('toolGroups') or [] if groups.exists(design, g)]
        loose = [o for o in self.objects if group_of(design, o) is None]
        return out, loose

    def comp_groups(self, comps, chosen):
        """[[id, name]] for a chips row: every group one of these components' own bodies
        is filed in (Extrude's _group_chips), then any chosen one that isn't."""
        design = self._design()
        folders, leaves = groups._doc_groups(design)
        ids, seen = [], set()
        for c in comps:
            if c is None or c.id in seen:
                continue
            seen.add(c.id)
            for k, _b in groups.keyed_bodies(c):
                g = groups.top(folders, leaves.get(k))
                if g in folders and g not in ids:
                    ids.append(g)
        ids += [g for g in chosen if g and g not in ids and g in folders]
        return [[g, group_name(design, g)] for g in ids]

    def chip_comps(self, tools):
        """The component(s) whose groups a chips row offers: the target's — and, for the
        tools, the clicked tools' — else the active one."""
        comps = [self.target_comp()]
        if tools:
            comps += [o.parentComponent for o in self.objects]
        comps = [c for c in comps if c is not None]
        if not comps:
            design = self._design()
            comps = [design.activeComponent] if design is not None else []
        return comps

    def target_set(self):
        """Every body the target stands for, or None when it is the one body clicked."""
        if self.target_is_comp():
            tc = self.target_comp()
            return solid_bodies(tc) if tc is not None else None
        g = self.target_group()
        return (group_bodies(self._design(), g) or []) if g else None

    def tool_comps(self):
        if self.spec.get('toolScope') != 'component':
            return []
        out, seen = [], set()
        for c in [o.parentComponent for o in self.objects] + [o.component for o in self.tool_occs]:
            if c.id not in seen:
                seen.add(c.id)
                out.append(c)
        return out

    def hints(self):
        """comp id -> the occurrence its bodies were clicked through."""
        out = {}
        for b in list(self.clicked.values()) + list(self.pick_raw.values()):
            try:
                occ = b.assemblyContext
                if occ is not None:
                    out.setdefault(fp.native(b).parentComponent.id, occ)
            except Exception:
                pass
        for o in self.tool_occs:
            out.setdefault(o.component.id, o)
        t = adsk.fusion.Occurrence.cast(self.pick_raw.get('target'))
        if t is not None:
            out.setdefault(t.component.id, t)
        return out

    def members(self):
        """(targets, tools) as native bodies, with why_not's reason when either is empty."""
        self.why = ''
        self.sets = (None, [])
        self.shown = ([], [])
        design = self._design()
        tc = self.target_comp() if self.target_is_comp() else None
        ocs = self.tool_comps()
        if tc is not None and any(c.id == tc.id for c in ocs):
            self.why = 'The target and the tools are the same component.'
            return [], []
        t = self.picks.get('target')
        tg = self.target_group()
        if (t is not None and tc is None and tg is None
                and self.spec.get('targetScope') == 'group'):
            self.why = '%s is not in a group.' % self.pick_label('target', t)
            return [], []
        self.seed_tool_groups()
        ogs, loose = self.tool_groups()
        if loose:
            self.why = '%s %s not in a group.' % (', '.join(b.name for b in loose),
                                                  'is' if len(loose) == 1 else 'are')
            return [], []
        if tg is not None and tg in ogs:
            self.why = 'The target and the tools are the same group.'
            return [], []
        self.sets = (tg, ogs)        # read now: execute consumes the tools
        targets = self.target_set()
        whole = targets is not None
        if targets is None:
            targets = [t] if adsk.fusion.BRepBody.cast(t) is not None else []
        if ocs:
            tools = [b for c in ocs for b in solid_bodies(c)]
        elif self.spec.get('toolScope') == 'group':
            # The chips are the truth: a clicked tool whose chip is off is no tool.
            tools = _union(group_bodies(design, g) for g in ogs)
            if not ogs and self.objects:
                self.why = 'Choose the tools’ groups.'
        else:
            tools = list(self.objects)
        if whole and not (ocs or ogs):
            targets = [b for b in targets if not any(b == t for t in tools)]
        else:
            tools = [b for b in tools if not any(b == t for t in targets)]
        self.counts = (len(targets), len(tools))
        if adsk.fusion.BRepBody.cast(t) is not None:
            hints = self.hints()
            self.shown = ([proxy(design, b, hints) for b in targets
                           if fp.token(b) != fp.token(t)] if whole else [],
                          [proxy(design, b, hints) for b in tools] if ogs else [])
        if whole and not targets:
            self.why = '%s has no bodies to combine.' % (
                tc.name if tc is not None else group_name(design, tg))
        elif (ocs or ogs) and not tools:
            self.why = '%s has no tool bodies.' % ', '.join(
                [c.name for c in ocs] + [group_name(design, g) for g in ogs])
        return targets, tools

    def linked(self):
        return self.target_is_comp() or bool(self.tool_comps()) or any(self.sets)

    def component(self):
        return self.target_comp()

    def extra(self):
        """The component rows' text, counted by members() before the preview's combines
        were built — after them, a consumed tool or a split target counts wrong."""
        out = {}
        tc = self.target_comp()
        ocs = self.tool_comps()
        n_t, n_o = self.counts or (None, None)
        tg = self.target_group()
        ogs = self.tool_groups()[0]
        design = self._design() if tg or ogs else None
        # The chips rows: the groups on offer, and the ones lit.
        if self.spec.get('targetScope') == 'group' and not self.target_is_comp():
            out['targetGroup'] = tg or ''
            out['targetGroupChips'] = self.comp_groups(self.chip_comps(False), [tg])
        if self.spec.get('toolScope') == 'group':
            out['toolGroups'] = ogs
            out['toolGroupsChips'] = self.comp_groups(self.chip_comps(True), ogs)
        if tc is not None and self.target_is_comp():
            n = n_t if n_t is not None else len(solid_bodies(tc))
            out['targetText'] = '%s · %s' % (
                tc.name, fp.count_text(n, ('body', 'bodies')) or 'no bodies')
        elif tg is not None:
            n = n_t if n_t is not None else len(group_bodies(design, tg) or [])
            out['targetText'] = '%s · %s' % (
                group_name(design, tg), fp.count_text(n, ('body', 'bodies')) or 'no bodies')
        if ocs or ogs:
            n = n_o
            if n is None:
                n = (sum(len(solid_bodies(c)) for c in ocs) if ocs
                     else len(_union(group_bodies(design, g) for g in ogs)))
            names = [c.name for c in ocs] if ocs else [group_name(design, g) for g in ogs]
            out['selectionText'] = '%s · %s' % (', '.join(names),
                                               fp.count_text(n, ('body', 'bodies'))
                                               or 'no bodies')
            out['hasSelection'] = True
        return out

    def start(self):
        """The first pre-selected thing is the target — a component when it is one (a
        Browser row); further components are tool components. With nothing, the first
        click is the target."""
        occs = [adsk.fusion.Occurrence.cast(e) for e in self.pre]
        occs = [o for o in occs if o is not None]
        first = adsk.fusion.Occurrence.cast(self.pre[0]) if self.pre else None
        if first is not None:
            self.set_pick('target', first.component, first)
            self.spec['targetScope'] = 'component'
            occs = occs[1:]
        elif self.objects:
            body = self.objects[0]
            raw = next((e for e in self.pre if fp.token(self.accept(e)) == fp.token(body)),
                       body)
            self.set_pick('target', body, raw)     # which takes it out of the tools
        else:
            self.armed = 'target'
        if occs:
            self.tool_occs = occs
            self.spec['toolScope'] = 'component'

    def set_pick(self, slot, entity, raw=None):
        super().set_pick(slot, entity, raw)
        if slot == 'target':
            self.spec['targetGroup'] = ''    # a clicked target: its own group, until a chip
        if slot == 'target' and self.lit:
            self.lit = None              # its pick_toks are new: light() fills them again
        # A new target can make tools of another component's bodies, or of itself.
        self.objects = self.narrow(self.objects)
        self.objects_id = tuple(fp.token(o) for o in self.objects)

    def take(self, ents, fresh):
        changed = super().take(ents, fresh)
        if changed:
            self.seed_tool_groups()
        return changed

    def spec_changed(self, before):
        """A target group chip: that group's first body (one in the target's component
        if it has one) becomes the target, which lights the rest. "Tools are: Group"
        chosen: the clicked tools' groups go on its chips."""
        if self.spec.get('toolScope') == 'group' and before.get('toolScope') != 'group':
            self.seed_tool_groups()
        g = self.spec.get('targetGroup')
        if not g or g == before.get('targetGroup'):
            return
        design = self._design()
        bodies = group_bodies(design, g) or []
        t = adsk.fusion.BRepBody.cast(self.picks.get('target'))
        if t is not None and any(b == t for b in bodies):
            self.lit = None              # the target stays; the group lit is another
            return
        tc = self.target_comp()
        body = next((b for b in bodies if tc is not None and b.parentComponent == tc),
                    bodies[0] if bodies else None)
        if body is None:
            self.spec['targetGroup'] = before.get('targetGroup') or ''
            return
        self.set_pick('target', body, proxy(design, body, self.hints()))
        self.spec['targetGroup'] = g
        self.lit = None                  # light() puts the new target in the selection

    def clear(self, what):
        if what == 'selection':
            self.tool_occs = []
            self.spec['toolGroups'] = []
            self.seen_tools = set()
        lit = self.lit
        super().clear(what)
        # The engine put back only the picks; a dropped target's lit bodies must go
        # before the next click reads them as tools. A fresh build lights them again.
        if what == 'selection' or lit:
            self.lit = None
            self.preview()

    def selectable(self, entity):
        # light() putting the target back is not a click on a pick.
        return self.relighting or super().selectable(entity)

    def light(self):
        """Show everything the combine takes as selected: the picks, the bodies clicked,
        and what members() read — with "Target is: Group / Component" every body of the
        target's group / component, with "Tools are: Group" every body of the groups on
        its chips — each as clicked through its occurrence. The target-side ones join its
        pick, so a click never takes one as a tool. A component picked from the Browser is lit
        already — its occurrence is the selection.

        Called from build(), before it makes anything: once a preview shows, a Join has
        consumed those bodies and they can't be read or selected. Puts back anything the
        selection lost since (a Browser click replaces it), not only what changed."""
        if self.sel_input is None:
            return
        lit_t, lit_o = self.shown
        toks = tuple(fp.token(b) for b in lit_t + lit_o)
        if toks == self.lit and tuple(fp.token(e) for e in self._selection()) == self.lit_sel:
            return
        self.lit = toks
        # A tool-group body lit last time and gone from the chips is no click of the user's.
        clicked = [self.clicked.get(fp.token(o), o) for o in self.objects
                   if fp.token(o) not in self.lit_tools]
        self.lit_tools = {fp.token(fp.native(b)) for b in lit_o}
        self.seen_tools |= self.lit_tools          # their groups are on the chips already
        self.relighting = True
        try:
            self.sel_input.clearSelection()
            for e in (list(self.pick_raw.values()) + list(self.tool_occs) + clicked
                      + lit_t + lit_o):
                try:
                    self.sel_input.addSelection(e)
                except Exception:
                    pass
        except Exception:
            self.log('light failed:', fp._tb())
        finally:
            self.relighting = False
        t = adsk.fusion.BRepBody.cast(self.picks.get('target'))
        if t is not None and 'target' in self.pick_toks:
            raw = self.pick_raw['target']
            self.pick_toks['target'] = ({fp.token(t), fp.token(raw)}
                                        | {fp.token(b) for b in lit_t}
                                        | {fp.token(fp.native(b)) for b in lit_t})
        self.lit_sel = self.sel_id = tuple(fp.token(e) for e in self._selection())

    def build(self, design):
        self.built = []
        targets, tools = self.members()
        self.light()
        steps = plan(self.spec.get('operation'), targets, tools,
                     self.spec.get('keep') == 'keep')
        if not steps:
            return None
        hints = self.hints()
        for tgt, tls, keep in steps:
            # A body clicked goes in as it was clicked, as before.
            tls = [self.clicked.get(fp.token(t), t) for t in tls]
            f = create(design, tgt, tls, self.spec.get('operation'), keep, hints, self.log)
            if f is None:
                return None
            self.built.append((f, tgt, tls))
        return self.built[0][0]

    def after_execute(self, result):
        link = None
        if self.linked():
            tc = self.target_comp() if self.target_is_comp() else None
            ocs = self.tool_comps()
            link = {'g': uuid.uuid4().hex[:12],
                    'op': self.spec.get('operation') or 'join',
                    'keep': self.spec.get('keep') or 'remove',
                    'tc': tc.id if tc is not None else '',
                    'oc': [c.id for c in ocs],
                    'tg': self.sets[0] or '',
                    'og': list(self.sets[1]),
                    'tt': [] if ocs or self.sets[1] else [_tok(b) for b in self.objects]}
        for f, tgt, tls in self.built:
            tag(f, link, tgt, tls)
        if link is not None:
            design = adsk.fusion.Design.cast(app.activeProduct)
            _set_fp(design, [f for f, _, _ in self.built])

    def why_not(self):
        if 'target' not in self.picks:
            return 'Click the target body first.'
        if self.why:
            return self.why
        if not self.objects and not self.tool_occs and not self.tool_groups()[0]:
            return 'Click the tool bodies.'
        return 'That combine will not build.'


_feature = None
_live = None


def run(context):
    global _feature, _live
    try:
        _feature = Combine()
        _feature.run()
        _live = Live()
        _live.start()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Combine run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _feature, _live
    try:
        if _feature is not None:
            _feature.stop()
    except Exception:
        pass
    try:
        if _live is not None:
            _live.stop()
    except Exception:
        pass
    _feature = None
    _live = None
