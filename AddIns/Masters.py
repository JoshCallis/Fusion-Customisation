"""
Masters — the master-sketch record and the mechanics of projecting one.

A MASTER SKETCH is an ordinary root-component sketch that other sketches project a
live, linked copy of (see the design note in chat — "Master sketches — shared contours
projected down the component tree"). This module is the shared mechanics: identifying
a master or a projection of one, projecting a master into a new sketch, keeping a
master ahead of everything that uses it in the timeline, creating one, finding every
sketch that holds a projection of it, and deleting one. Masters are always hidden
except the one being edited — Visibility Snapshot.py's _rehide_bounds_sketches
enforces that; there is no switch. The add-in (`Master Sketches/Master Sketches.py`)
is the UI on top of this — Convert, Edit Original, Delete Master, Convert to Sketch, selection
grouping, whole-projection delete.

IDENTITY IS THE ATTRIBUTE, NEVER THE NAME
──────────────────────────────────────────
A master sketch carries FusionMasters/master = {"id": uuid, "created": ts}. A
projected entity carries FusionMasters/instance = {"master": id, "instance": uuid} —
written on every entity ONE projection creates, so two projections of different
masters (or two projections of the SAME master, pasted twice — refused today, but the
attribute doesn't assume that) sitting in one sketch stay distinguishable. The name
only gets the "Master " prefix (NAME_PREFIX) so the browser filter and the visibility
policy (Visibility Snapshot.py's _is_master_sketch) can work without loading this
whole module — see Bounds.py's SKETCH_PREFIX for the precedent. Renaming a master
sketch does not break anything here; it would only stop matching that faster check.

SPIKE FINDINGS THIS MODULE RELIES ON
────────────────────────────────────
  • Sketch.project2 takes the entity as a LIST: project2([entity], isLinked) —
    project2(entity, isLinked) raises a SWIG vector-argument TypeError.
  • It returns a SketchEntityVector, not an ObjectCollection: no .count/.item(),
    only indexing/iteration. _project_result_list below normalises that.
  • referencedEntity on the result resolves back to the source entity, and — verified
    BY HAND, not by the script (the script's own doEvents()+poll was too impatient for
    a heavy real document) — the projection's geometry does follow a later edit to the
    master after a real recompute.
  • TimelineObject.reorder(index) works to move a brand-new root sketch earlier in the
    timeline with no sketch open for edit. Reordering while a DIFFERENT sketch is open
    for edit was not exercised live; ensure_before below still tries it and reports
    whatever Fusion says rather than assuming either way.
  • No native Fusion command has "edit"+"original" in its name or id — nothing for
    MasterEditOriginalCmd (in the add-in) to intercept; it has to be entirely new.
"""

import json
import os
import time
import types as _types
import uuid

import adsk.core
import adsk.fusion

_ADDINS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


# The plane/frame/proxy arithmetic and the selection closure already live in Sketch
# Clipboard.py; re-executing that file (rather than duplicating its helpers) keeps the
# two in agreement about what "the same world position" and "an identity transform"
# mean. Its own run()/stop() are never called here — only the pure functions are used.
sc = _load('sc', os.path.join('Sketch Clipboard', 'Sketch Clipboard.py'))

try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None

ATTR_GROUP = 'FusionMasters'
MASTER_KEY = 'master'
INSTANCE_KEY = 'instance'
# On a master's bounds ANCHOR points (Convert to Master): construction points pinned
# to the origin by a component's bound parameters. Scaffolding inside the master,
# never projected out of it.
ANCHOR_KEY = 'anchor'
NAME_PREFIX = 'Master '

TOL = 1e-4


# ── Identity ─────────────────────────────────────────────────────────────────

def _read_attr(entity, key):
    try:
        attr = entity.attributes.itemByName(ATTR_GROUP, key)
        if attr is None or not attr.value:
            return None
        val = json.loads(attr.value)
        return val if isinstance(val, dict) else None
    except Exception:
        return None


def is_master(sketch):
    """True if *sketch* is a master — decided by the attribute, never the name."""
    return _read_attr(sketch, MASTER_KEY) is not None


def master_id(sketch):
    """The master's uuid, or None if *sketch* isn't one."""
    rec = _read_attr(sketch, MASTER_KEY)
    return rec.get('id') if rec else None


def find_master(design, mid):
    """The master sketch with id *mid*. Masters only ever live in the root."""
    try:
        for sk in design.rootComponent.sketches:
            if master_id(sk) == mid:
                return sk
    except Exception:
        pass
    return None


def all_masters(design):
    """Every master sketch in the root, in no particular order."""
    out = []
    try:
        for sk in design.rootComponent.sketches:
            if is_master(sk):
                out.append(sk)
    except Exception:
        pass
    return out


def projection_of(entity):
    """(master_id, instance_id) for a projected entity, or None if it isn't one.

    Falls back to the entity's referencedEntity when the instance attribute itself
    is missing — e.g. Fusion's own re-project-on-repair path, or geometry from
    before this attribute existed — identifying the master but not a specific
    instance grouping (instance_id comes back None in that case).
    """
    rec = _read_attr(entity, INSTANCE_KEY)
    if rec is not None:
        return rec.get('master'), rec.get('instance')
    try:
        ref = entity.referencedEntity
        parent = ref.parentSketch if ref is not None else None
    except Exception:
        parent = None
    if parent is not None and is_master(parent):
        return master_id(parent), None
    return None


def instance_entities(sketch, instance_id):
    """Every entity in *sketch* that belongs to the projection *instance_id*."""
    out = []
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for e in coll:
            _, inst = projection_of(e) or (None, None)
            if inst == instance_id:
                out.append(e)
    return out


def _source_sketch(entity):
    """The sketch a projected entity was projected from, or None."""
    try:
        if not entity.isReference:
            return None
        ref = entity.referencedEntity
        return ref.parentSketch if ref is not None else None
    except Exception:
        return None


def reference_group(sketch, entity):
    """Every projected entity in *sketch* — the whole reference *entity* belongs to.

    A tagged master instance groups by instance id; any other projection (plain
    project(), Section tethers, repaired projections) groups by source sketch.
    Returns [] when *entity* isn't a projection.
    """
    _, inst = projection_of(entity) or (None, None)
    if inst is not None:
        return instance_entities(sketch, inst)
    src = _source_sketch(entity)
    if src is None:
        return []
    out = []
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for e in coll:
            if _source_sketch(e) == src:
                out.append(e)
    return out


def make_solid(entities):
    """Drop construction on projected entities so Fusion draws them solid (in the
    environment's SketchProjectedLineColor) instead of dashed."""
    for e in entities:
        try:
            if e.isReference and getattr(e, 'isConstruction', False):
                e.isConstruction = False
        except Exception:
            pass


# ── Timeline ordering ───────────────────────────────────────────────────────

def ensure_before(design, master, sketch):
    """Move *master* earlier in the timeline if it isn't already before *sketch*.

    Returns a list of problems (empty on success, including when nothing needed to
    move). Never raises: a reorder Fusion refuses — because *sketch* is one of the
    master's own dependencies, directly or through the root origin / a Bounds
    sketch — comes back as a problem string, in Fusion's own words, rather than a
    guess about why.
    """
    try:
        m_tl = master.timelineObject
        s_tl = (sketch.nativeObject or sketch).timelineObject
    except Exception as e:
        return ['could not read timeline position: %s' % e]
    if m_tl is None or s_tl is None:
        return []                      # one of them predates the timeline; nothing to do
    if m_tl.index < s_tl.index:
        return []
    try:
        m_tl.reorder(s_tl.index)
        return []
    except Exception as e:
        return ['could not move "%s" earlier than "%s": %s'
                % (master.name, sketch.name, e)]


# ── Creating a master ────────────────────────────────────────────────────────

def create_master(design, label, before_index, local=None):
    """A new, hidden, tagged root sketch, positioned just before *before_index* in
    the timeline. *local* is the wanted frame in the root's own coordinates
    (an adsk.core.Matrix3D); omit it for the root XY plane. Returns
    (master_sketch_or_None, extras, problems) — *extras* are any construction
    geometry _plane_for had to build to reach an offset/rotated frame, and must
    outlive the sketch the same way Sketch Clipboard's paste keeps them.
    """
    root = design.rootComponent
    problems = []
    extras = []

    if local is None or sc._same(sc._ORIGIN_PLANE_FRAMES['XY'], local):
        plane = root.xYConstructionPlane
    else:
        plane, extras, note = sc._plane_for(root, local)
        if plane is None:
            return None, [], [note or 'could not find a plane for this master']

    timeline = design.timeline
    saved = None
    count_before = None
    try:
        saved = timeline.markerPosition
        count_before = timeline.count
        timeline.markerPosition = before_index
    except Exception as e:
        problems.append('could not move to timeline position %d: %s' % (before_index, e))

    master_sk = None
    try:
        master_sk = root.sketches.add(plane)
        master_sk.name = label
        try:
            master_sk.isVisible = False
        except Exception as e:
            problems.append('could not hide the master: %s' % e)
        record = {'id': str(uuid.uuid4()), 'created': time.time()}
        try:
            master_sk.attributes.add(ATTR_GROUP, MASTER_KEY, json.dumps(record))
        except Exception as e:
            problems.append('could not tag the master: %s' % e)
    except Exception as e:
        problems.append('could not create the master sketch: %s' % e)

    # PUTTING THE MARKER BACK IS NOT COSMETIC — it is where this used to eat a
    # sketch. Inserting the master at *before_index* pushes every later timeline
    # item — including the very sketch we were called for — up by one, so
    # restoring the marker to the raw *saved* leaves the last previously computed
    # item rolled back. The symptom is nasty and doesn't look like a timeline
    # problem: the sketch disappears from the browser (native and ours) while its
    # component still shows the earlier ones, F won't open it for edit, and the
    # master's own curves sit at the same world position in the viewport so it
    # still looks like the sketch is there. Shift by however many items the insert
    # actually added, and report a failure instead of swallowing it.
    if saved is not None:
        try:
            added = (timeline.count - count_before) if count_before is not None else 1
            target = saved + added if before_index <= saved else saved
            timeline.markerPosition = min(target, timeline.count)
        except Exception as e:
            problems.append('could not put the timeline marker back (features after '
                            'position %d may still be rolled back): %s' % (before_index, e))

    return master_sk, extras, problems


# ── Projecting a master into a sketch ───────────────────────────────────────

def _project_result_list(res):
    """Whatever project2 handed back, as a plain list of entities.

    Confirmed live: it's a SketchEntityVector with no .count/.item — indexing and
    iteration both work, so try count/.item first (for whatever future call shape
    might use an ObjectCollection instead) and fall back to iterating.
    """
    if res is None:
        return []
    try:
        if res.count:
            return [res.item(i) for i in range(res.count)]
    except Exception:
        pass
    try:
        return list(res)
    except Exception:
        return []


def _world_normal(sketch):
    """The world-space normal of *sketch*'s plane, or None."""
    try:
        native = sketch.nativeObject or sketch
        occ = sketch.assemblyContext
        m = sc._world_matrix(native, occ)
    except Exception:
        return None
    try:
        _, xdir, ydir = sc._frame_of(m)
        return xdir.crossProduct(ydir)
    except Exception:
        return None


def _already_holds(sketch, mid):
    """True if *sketch* already has a projection of master *mid*."""
    for e in list(sketch.sketchCurves) + list(sketch.sketchPoints):
        if not getattr(e, 'isReference', False):
            continue
        m, _inst = projection_of(e) or (None, None)
        if m == mid:
            return True
    return False


def holders(design, mid):
    """Every sketch, in any component, that already holds a projection of master
    *mid* — the Delete Master / Convert to Sketch prompts' "also projected into"
    list."""
    out = []
    try:
        for comp in design.allComponents:
            for sk in comp.sketches:
                if _already_holds(sk, mid):
                    out.append(sk)
    except Exception:
        pass
    return out


def delete_master(design, master):
    """Delete *master* itself, plus best-effort removal of its own reference
    plane when it is a non-origin ConstructionPlane that create_master/
    sc._plane_for built and nothing else uses. Returns the problems it hit.
    """
    problems = []
    plane = None
    try:
        plane = adsk.fusion.ConstructionPlane.cast(master.referencePlane)
    except Exception:
        plane = None

    try:
        master.deleteMe()
    except Exception as e:
        problems.append('could not delete "%s": %s' % (master.name, e))
        return problems

    if plane is None:
        return problems
    try:
        root = design.rootComponent
        is_origin_plane = any(plane == p for p in (
            root.xYConstructionPlane, root.xZConstructionPlane, root.yZConstructionPlane))
    except Exception:
        is_origin_plane = False
    if is_origin_plane:
        return problems
    try:
        if plane.isValid:
            plane.deleteMe()
    except Exception:
        pass                               # still used by something else — leave it
    return problems


def project_master(design, master, tgt_sketch):
    """Project every curve/point of *master* (its origin excluded) into
    *tgt_sketch*, live-linked. Returns (entities, problems); *entities* is empty
    when refused, with the reason as the sole problem.
    """
    if is_master(tgt_sketch):
        return [], ['a master cannot project into another master — nesting is not '
                     'supported yet']

    mid = master_id(master)
    if mid is None:
        return [], ['"%s" is not a master' % master.name]

    if _already_holds(tgt_sketch, mid):
        return [], ['"%s" already holds this master' % tgt_sketch.name]

    m_normal = _world_normal(master)
    t_normal = _world_normal(tgt_sketch)
    if m_normal is None or t_normal is None:
        return [], ['could not read the plane of one of the sketches']
    if abs(m_normal.dotProduct(t_normal)) < 1.0 - 1e-6:
        return [], ['"%s" is not parallel to the master\'s plane' % tgt_sketch.name]

    problems = ensure_before(design, master, tgt_sketch)
    if problems and find_master(design, mid) is None:
        return [], problems             # the master itself is gone somehow; bail

    to_project = [e for e in list(master.sketchCurves) + list(master.sketchPoints)
                  if e != master.originPoint and not getattr(e, 'isReference', False)
                  and e.attributes.itemByName(ATTR_GROUP, ANCHOR_KEY) is None]
    if not to_project:
        return [], problems + ['the master has no geometry to project']

    try:
        raw = tgt_sketch.project2(to_project, True)
    except Exception as e:
        return [], problems + ['project2 failed: %s' % e]

    entities = _project_result_list(raw)
    if not entities:
        return [], problems + ['project2 returned nothing']

    make_solid(entities)
    instance_id = str(uuid.uuid4())
    for e in entities:
        try:
            e.attributes.add(ATTR_GROUP, INSTANCE_KEY,
                              json.dumps({'master': mid, 'instance': instance_id}))
        except Exception as ex:
            problems.append('could not tag a projected entity: %s' % ex)

    return entities, problems
